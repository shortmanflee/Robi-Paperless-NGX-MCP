from __future__ import annotations

import base64

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from paperless_mcp import ingest
from paperless_mcp.ingest import UnsupportedFile, detect_mime

PDF = b"%PDF-1.7\n" + b"0" * 100
TASK = "8f6c1c5e-0000-4000-8000-000000000001"


def task(status: str, **result) -> dict:
    return {
        "id": 77,
        "task_id": TASK,
        "task_type": "consume_file",
        "status": status,
        "result_data": result or None,
        "input_data": {"filename": "bill.pdf"},
    }


def tasks_page(*rows: dict) -> Response:
    return Response(200, json={"count": len(rows), "next": None, "results": list(rows)})


@pytest.mark.parametrize(
    ("head", "name", "expected"),
    [
        (PDF, "x.bin", "application/pdf"),
        (b"\x89PNG\r\n\x1a\nrest", "a", "image/png"),
        (b"\xff\xd8\xff\xe0rest", "a", "image/jpeg"),
        (b"II*\x00rest", "a", "image/tiff"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "a", "image/webp"),
        (b"BM\x10\x00\x00\x00\x00\x00\x00\x00", "a", "image/bmp"),
        (b"BMW invoice text", "notes.txt", "text/plain"),
        (b"PK\x03\x04rest", "letter.docx", "application/vnd.openxmlformats-officedocument."
         "wordprocessingml.document"),
        (b"<!DOCTYPE html><html>", "page", "text/html"),
        (b"hello", "x.csv", "text/csv"),
        (b"From: a@b\nSubject: hi\n", "mail.eml", "message/rfc822"),
    ],
)  # fmt: skip
def test_detect_mime(tmp_path, head, name, expected):
    p = tmp_path / "f"
    p.write_bytes(head)
    assert detect_mime(p, name) == expected


@pytest.mark.parametrize(
    ("head", "name", "match"),
    [
        (b"", "a.pdf", "empty"),
        (b"PK\x03\x04rest", "archive.zip", "not a recognised office"),
        (b"\x7fELF\x02\x01\x01\x00\x00", "prog", "binary format"),
    ],
)
def test_detect_mime_rejects(tmp_path, head, name, match):
    p = tmp_path / "f"
    p.write_bytes(head)
    with pytest.raises(UnsupportedFile, match=match):
        detect_mime(p, name)


async def test_upload_document_sends_multipart_and_waits(mcp_client, pl_mock, store):
    post = pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    pl_mock.get("/tasks/").mock(return_value=tasks_page(task("success", document_id=555)))
    res = await mcp_client.call_tool(
        "upload_document",
        {
            "filename": "bill",
            "content_base64": base64.b64encode(PDF).decode(),
            "title": "March bill",
            "tags": ["Invoice", "tax"],
            "correspondent": "ACME",
            "custom_fields": {"Status": "Paid"},
            "wait_seconds": 5,
        },
    )
    raw = post.calls[0].request.content
    assert b'filename="bill.pdf"' in raw  # extension added from detected type
    assert b"Content-Type: application/pdf" in raw and PDF in raw
    assert raw.count(b'name="tags"') == 2
    assert b'name="tags"\r\n\r\n1\r\n' in raw and b'name="tags"\r\n\r\n3\r\n' in raw
    assert b'name="correspondent"\r\n\r\n5\r\n' in raw
    assert b'name="custom_fields"\r\n\r\n{"40": "opt1"}\r\n' in raw
    assert res.data["document_id"] == 555
    assert res.data["status"] == "success"
    assert list(store.dir.iterdir()) == []  # temp file cleaned up


async def test_upload_reports_duplicate(mcp_client, pl_mock):
    pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    pl_mock.get("/tasks/").mock(
        return_value=tasks_page(task("failure", duplicate_of=12, reason="Not consuming: dup"))
    )
    res = await mcp_client.call_tool(
        "upload_document",
        {"filename": "a.pdf", "content_base64": base64.b64encode(PDF).decode()},
    )
    assert res.data["status"] == "failure"
    assert res.data["duplicate_of"] == 12
    assert "dup" in res.data["error"]


async def test_upload_wait_times_out_with_hint(mcp_client, pl_mock, monkeypatch):
    monkeypatch.setattr(ingest, "POLL_INTERVAL", 0.01)
    pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    pl_mock.get("/tasks/").mock(return_value=tasks_page(task("started")))
    res = await mcp_client.call_tool(
        "upload_document",
        {
            "filename": "a.pdf",
            "content_base64": base64.b64encode(PDF).decode(),
            "wait_seconds": 0.05,
        },
    )
    assert res.data["status"] == "started"
    assert "get_task" in res.data["hint"]


async def test_upload_without_wait_returns_task(mcp_client, pl_mock):
    pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    tasks = pl_mock.get("/tasks/").mock(return_value=tasks_page())
    res = await mcp_client.call_tool(
        "upload_document",
        {"filename": "a.pdf", "content_base64": base64.b64encode(PDF).decode(), "wait_seconds": 0},
    )
    assert res.data == {
        "task_id": TASK,
        "status": "pending",
        "filename": "a.pdf",
        "size_bytes": 109,
    }
    assert not tasks.called


async def test_upload_inline_cap(server_factory):
    async with Client(server_factory(mcp_max_inline_mb=0.00001)) as c:
        with pytest.raises(ToolError, match="create_upload_link"):
            await c.call_tool(
                "upload_document",
                {"filename": "a.pdf", "content_base64": base64.b64encode(PDF).decode()},
            )


async def test_upload_unknown_tag_fails_before_posting(mcp_client, pl_mock):
    post = pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    with pytest.raises(ToolError, match="no tag named 'Nope'"):
        await mcp_client.call_tool(
            "upload_document",
            {
                "filename": "a.pdf",
                "content_base64": base64.b64encode(PDF).decode(),
                "tags": ["Nope"],
            },
        )
    assert not post.called


async def test_get_task(mcp_client, pl_mock):
    route = pl_mock.get("/tasks/").mock(return_value=tasks_page(task("success", document_id=9)))
    res = await mcp_client.call_tool("get_task", {"task_id": TASK})
    assert route.calls[0].request.url.params["task_id"] == TASK
    assert res.data["document_id"] == 9


async def test_upload_from_path_stdio_only(server_factory, pl_mock, tmp_path):
    pdf = tmp_path / "docs" / "scan.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(PDF)
    post = pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    pl_mock.get("/tasks/").mock(return_value=tasks_page(task("success", document_id=1)))

    async with Client(server_factory()) as c:
        assert "upload_document_from_path" not in {t.name for t in await c.list_tools()}

    stdio = server_factory(
        mcp_transport="stdio", mcp_local_upload_roots=str(pdf.parent), mcp_auth_tokens=None
    )
    async with Client(stdio) as c:
        names = {t.name for t in await c.list_tools()}
        assert "create_upload_link" not in names
        res = await c.call_tool("upload_document_from_path", {"path": str(pdf)})
        assert res.data["document_id"] == 1
        assert b'filename="scan.pdf"' in post.calls[0].request.content
        outside = tmp_path / "secret.pdf"
        outside.write_bytes(PDF)
        with pytest.raises(ToolError, match="outside the allowed folders"):
            await c.call_tool("upload_document_from_path", {"path": str(outside)})
    assert pdf.exists()  # the user's file is never deleted
