"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .review import ReviewService
from .service import DomainService
from .storage import Database


def get_review_service(service: DomainService) -> ReviewService:
    """复用基础服务的数据库与时钟创建盲审服务（带缓存）。"""

    cached = getattr(service, "_review_service", None)
    if cached is None:
        cached = ReviewService(service.database, service.clock)
        service._review_service = cached
    return cached


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          review_service: ReviewService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    review = review_service or get_review_service(service)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _route_review(review, method, parsed, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt_response(receipt, created_status: int = 201) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.replayed else created_status, receipt.__dict__


def _route_review(review: ReviewService, method: str, parsed, body: dict[str, Any],
                  actor_id: str) -> tuple[int | None, dict[str, Any]]:
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0] != "review":
        return None, {}

    if method == "POST" and parts == ["review", "rule-versions"]:
        return _receipt_response(review.register_rule_version(actor_id=actor_id, **body))
    if method == "POST" and parts == ["review", "tracks"]:
        return _receipt_response(review.register_track(actor_id=actor_id, **body))
    if method == "POST" and parts == ["review", "reviewers"]:
        return _receipt_response(review.register_reviewer(actor_id=actor_id, **body))
    if method == "POST" and parts == ["review", "works"]:
        return _receipt_response(review.register_work(actor_id=actor_id, **body))
    if method == "POST" and parts == ["review", "relations"]:
        return _receipt_response(review.declare_relation(actor_id=actor_id, **body))
    if method == "POST" and parts == ["review", "batches"]:
        return _receipt_response(review.create_batch(actor_id=actor_id, **body))
    if method == "GET" and parts == ["review", "my-tasks"]:
        return 200, review.list_my_tasks(actor_id)
    if len(parts) == 4 and parts[:2] == ["review", "tasks"]:
        task_id = parts[2]
        if method == "GET" and parts[3] == "material":
            return 200, review.get_task_material(actor_id, task_id)
        if method == "POST" and parts[3] == "claim":
            return _receipt_response(review.claim_task(actor_id=actor_id, task_id=task_id, **body))
        if method == "POST" and parts[3] == "scores":
            return _receipt_response(review.submit_score(actor_id=actor_id, task_id=task_id, **body))
        if method == "POST" and parts[3] == "recuse":
            return _receipt_response(review.recuse_task(actor_id=actor_id, task_id=task_id, **body))
        if method == "POST" and parts[3] == "invalidate":
            return _receipt_response(review.invalidate_score(actor_id=actor_id, task_id=task_id, **body))
    if len(parts) == 3 and parts[:2] == ["review", "snapshots"] and method == "GET":
        return 200, review.get_snapshot(actor_id, parts[2])
    if len(parts) >= 3 and parts[:2] == ["review", "batches"]:
        batch_id = parts[2]
        if len(parts) == 4:
            action = parts[3]
            if method == "POST" and action == "works":
                return _receipt_response(review.add_work_to_batch(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "exceptions":
                return _receipt_response(review.grant_exception(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "plan":
                return _receipt_response(review.generate_plan(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "publish":
                return _receipt_response(review.publish_batch(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "seal":
                return _receipt_response(review.seal_batch(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "absences":
                return _receipt_response(review.mark_absent(
                    actor_id=actor_id, batch_id=batch_id, **body))
            if method == "POST" and action == "emergency-assignments":
                return _receipt_response(review.emergency_assign(
                    actor_id=actor_id, batch_id=batch_id, **body))
        if len(parts) == 4 and parts[3] == "report" and method == "GET":
            return 200, review.get_batch_report(actor_id, batch_id)
        if (len(parts) == 6 and parts[3] == "works" and parts[5] == "seal"
                and method == "POST"):
            return _receipt_response(review.seal_work(
                actor_id=actor_id, batch_id=batch_id, work_id=parts[4], **body))
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
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
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    service = DomainService(database)
    get_review_service(service)  # 提前建立盲审表结构
    Handler.service = service
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
