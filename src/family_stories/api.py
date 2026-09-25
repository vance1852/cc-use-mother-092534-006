"""家风故事服务的 HTTP/JSON 边界，复用基础层路由。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from festival_foundation.api import route as foundation_route
from festival_foundation.errors import DomainError, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .service import FamilyStoryService


def route(family: FamilyStoryService, foundation: DomainService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把家风故事请求分派到领域服务，其余路径回落到基础层。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    parts = [segment for segment in parsed.path.split("/") if segment]
    try:
        if parts and parts[0] == "stories":
            return _story_route(family, method, parts, query, actor_id, body)
        if parts and parts[0] == "review-queue" and method == "GET":
            stage = query.get("stage", [None])[0]
            return 200, {"items": family.review_queue(actor_id=actor_id, stage=stage)}
        if parts and parts[0] == "published" and method == "GET":
            view_scope = query.get("view_scope", [""])[0]
            if not view_scope:
                raise ValidationError("view_scope 不能为空")
            return 200, family.list_published(actor_id=actor_id, view_scope=view_scope)
        if parts and parts[0] == "access-audit" and method == "GET":
            story_id = query.get("story_id", [None])[0]
            return 200, {"items": family.list_access_audit(actor_id=actor_id, story_id=story_id)}
        if parts and parts[0] == "batches" and method == "POST":
            receipt = family.batch_import(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        return foundation_route(foundation, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _story_route(family: FamilyStoryService, method: str, parts: list[str],
                 query: dict[str, list[str]], actor_id: str,
                 body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if len(parts) == 1 and method == "POST":
        receipt = family.submit_story(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if len(parts) < 2:
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    story_id = parts[1]
    tail = parts[2] if len(parts) > 2 else ""
    if not tail and method == "GET":
        version = query.get("version", [None])[0]
        view_scope = query.get("view_scope", [None])[0]
        result = family.view_story(actor_id=actor_id, story_id=story_id,
                                   version=int(version) if version else None,
                                   view_scope=view_scope)
        return 200, result
    if tail == "view" and method == "GET":
        view_scope = query.get("view_scope", [""])[0]
        if not view_scope:
            raise ValidationError("view_scope 不能为空")
        result = family.view_story(actor_id=actor_id, story_id=story_id, view_scope=view_scope)
        return 200, result
    if tail == "explain" and method == "GET":
        version = query.get("version", [None])[0]
        result = family.explain_story(actor_id=actor_id, story_id=story_id,
                                      version=int(version) if version else None)
        return 200, result
    if tail == "revisions" and method == "POST":
        receipt = family.revise_story(actor_id=actor_id, story_id=story_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if tail == "consents" and method == "POST":
        receipt = family.record_consent(actor_id=actor_id, story_id=story_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if tail == "consents:narrow" and method == "POST":
        receipt = family.narrow_consent(actor_id=actor_id, story_id=story_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if tail == "review-claims" and method == "POST":
        stage = body.get("stage")
        return 200, family.claim_review(actor_id=actor_id, story_id=story_id, stage=stage)
    if tail == "review-decisions" and method == "POST":
        receipt = family.decide_review(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if tail == "publication" and method == "POST":
        receipt = family.publish_story(actor_id=actor_id, story_id=story_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    family: FamilyStoryService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.family, self.foundation, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动家风故事 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动家风故事采集与分级开放服务")
    parser.add_argument("--database", default="family_stories.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--lease-seconds", type=int, default=900)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.foundation = DomainService(database)
    Handler.family = FamilyStoryService(database, lease_seconds=args.lease_seconds)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
