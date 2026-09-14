"""Mocked tests for the Microsoft Graph ingestion connector.

Nothing here touches the network. The single network seam in
``archivepilot/graph.py`` is :meth:`GraphClient._request`, and every test
replaces it with a stub transport that replays scripted responses and
records the calls made. Credentials below are obvious placeholders — no
real tenant, app id or secret appears anywhere in this repo.
"""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
import urllib.parse
import zipfile

from archivepilot import graph, ingest
from archivepilot.db import Archive
from archivepilot.graph import (GraphClient, GraphCredentials, GraphError,
                                HttpResponse, MissingCredentials)
from archivepilot.ingest import (extract_text, folder_path, ingest_sharepoint,
                                 ingest_teams, is_indexable, main,
                                 map_drive_item, map_teams_message, strip_markup)

TENANT = "contoso-tenant-id"
CLIENT_ID = "app-client-id"
SECRET = "placeholder-secret-value"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"

FAKE_ENV = {
    "MSGRAPH_TENANT_ID": TENANT,
    "MSGRAPH_CLIENT_ID": CLIENT_ID,
    "MSGRAPH_CLIENT_SECRET": SECRET,
}
CRED_VARS = tuple(FAKE_ENV)


# ----------------------------------------------------------------------
# test doubles
# ----------------------------------------------------------------------
def response(status=200, body=None, headers=None):
    """Build an HttpResponse the way the real transport would."""
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    elif body is None:
        body = b""
    return HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, body)


def token_response(token="tok-1", expires_in=3600):
    return response(200, {"access_token": token, "expires_in": expires_in,
                          "token_type": "Bearer"})


class StubTransport:
    """Stands in for ``GraphClient._request``: scripted, ordered, recorded."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _pick(self, method, url, data):
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)

    def __call__(self, method, url, *, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "data": data})
        picked = self._pick(method, url, data)
        if callable(picked):
            picked = picked(method, url, data)
        return picked

    @property
    def urls(self):
        return [call["url"] for call in self.calls]

    @property
    def methods(self):
        return [call["method"] for call in self.calls]


class RoutingTransport(StubTransport):
    """Answers by URL regex, in order — so multi-step flows read clearly."""

    def __init__(self, routes):
        super().__init__()
        self.routes = list(routes)

    def _pick(self, method, url, data):
        for pattern, item in self.routes:
            if re.search(pattern, url):
                return item
        raise AssertionError(f"no stub route for {method} {url}")


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSleep:
    """Records requested backoff instead of actually sleeping."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def make_client(*, transport=None, clock=None, sleeper=None, **kwargs):
    """A real GraphClient whose transport, clock and sleep are all stubbed."""
    client = GraphClient(GraphCredentials(TENANT, CLIENT_ID, SECRET),
                         clock=clock or FakeClock(),
                         sleep=sleeper or FakeSleep(), **kwargs)
    # setattr (not direct assignment) so the stubbed transport is accepted
    # wherever the real signature is expected.
    setattr(client, "_request", transport or StubTransport())
    return client


@contextlib.contextmanager
def env_without_graph_creds():
    saved = {name: os.environ.pop(name, None) for name in CRED_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


@contextlib.contextmanager
def graph_env(env=None):
    saved = {name: os.environ.get(name) for name in CRED_VARS}
    os.environ.update(env or FAKE_ENV)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def patched_ingest_client(transport):
    """Make the CLI's run() use our stubbed client (never the real network)."""
    client = make_client(transport=transport)
    original = ingest.GraphClient
    ingest.GraphClient = lambda *args, **kwargs: client
    try:
        yield client
    finally:
        ingest.GraphClient = original


def mapped(record):
    """Assert a mapper produced a record, and return it (narrows the type)."""
    if record is None:
        raise AssertionError("expected a mapped record, got None")
    return record


def make_docx(paragraphs):
    """A minimal but valid enough .docx (zip + WordprocessingML)."""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("[Content_Types].xml", "<Types/>")
        bundle.writestr("word/document.xml", document)
    return buffer.getvalue()


# ----------------------------------------------------------------------
# auth: token fetch, caching, refresh
# ----------------------------------------------------------------------
class TokenTests(unittest.TestCase):
    def test_token_is_fetched_once_then_served_from_cache(self):
        transport = StubTransport(token_response())
        clock = FakeClock()
        client = make_client(transport=transport, clock=clock)

        self.assertEqual(client.token(), "tok-1")
        clock.advance(30)
        self.assertEqual(client.token(), "tok-1")
        self.assertEqual(client.token(), "tok-1")

        self.assertEqual(len(transport.calls), 1, "cache must prevent a 2nd call")
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], TOKEN_URL)
        self.assertNotIn("Authorization", call["headers"], "token call must not self-auth")
        form = dict(urllib.parse.parse_qsl(call["data"].decode("utf-8")))
        self.assertEqual(form["grant_type"], "client_credentials")
        self.assertEqual(form["scope"], "https://graph.microsoft.com/.default")
        self.assertEqual(form["client_id"], CLIENT_ID)
        self.assertEqual(form["client_secret"], SECRET)

    def test_token_is_refreshed_after_expiry(self):
        transport = StubTransport(token_response("tok-1"), token_response("tok-2"))
        clock = FakeClock()
        client = make_client(transport=transport, clock=clock)

        self.assertEqual(client.token(), "tok-1")
        clock.advance(3600)  # past expires_in
        self.assertEqual(client.token(), "tok-2")
        self.assertEqual(len(transport.calls), 2)

    def test_token_refreshes_inside_the_skew_window(self):
        transport = StubTransport(token_response("tok-1"), token_response("tok-2"))
        clock = FakeClock()
        client = make_client(transport=transport, clock=clock)

        client.token()  # ttl = 3600 - 60s skew = 3540
        clock.advance(3539)
        self.assertEqual(client.token(), "tok-1")
        self.assertEqual(len(transport.calls), 1)
        clock.advance(2)
        self.assertEqual(client.token(), "tok-2")
        self.assertEqual(len(transport.calls), 2)

    def test_token_failure_reports_graph_error_without_the_secret(self):
        transport = StubTransport(response(401, {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.",
        }))
        client = make_client(transport=transport)

        with self.assertRaises(GraphError) as caught:
            client.token()
        message = str(caught.exception)
        self.assertIn("invalid_client", message)
        self.assertIn("HTTP 401", message)
        self.assertNotIn(SECRET, message)

    def test_credentials_repr_never_shows_the_secret(self):
        credentials = GraphCredentials(TENANT, CLIENT_ID, SECRET)
        self.assertNotIn(SECRET, repr(credentials))
        self.assertIn("client_secret='***'", repr(credentials))


# ----------------------------------------------------------------------
# paging
# ----------------------------------------------------------------------
class PagingTests(unittest.TestCase):
    def test_paging_follows_two_next_links(self):
        page1 = {"value": [{"id": "m1"}, {"id": "m2"}],
                 "@odata.nextLink": f"{GRAPH}/teams/t1/channels/c1/messages?$skiptoken=p2"}
        page2 = {"value": [{"id": "m3"}],
                 "@odata.nextLink": f"{GRAPH}/teams/t1/channels/c1/messages?$skiptoken=p3"}
        page3 = {"value": [{"id": "m4"}]}
        transport = StubTransport(token_response(), response(200, page1),
                                  response(200, page2), response(200, page3))
        client = make_client(transport=transport)

        ids = [message["id"] for message in client.iter_channel_messages("t1", "c1")]

        self.assertEqual(ids, ["m1", "m2", "m3", "m4"])
        self.assertEqual(len(transport.calls), 4, "1 token call + 3 pages")
        self.assertIn("$skiptoken=p2", transport.urls[2])
        self.assertIn("$skiptoken=p3", transport.urls[3])

    def test_collection_limit_stops_early(self):
        page1 = {"value": [{"id": "m1"}, {"id": "m2"}],
                 "@odata.nextLink": f"{GRAPH}/teams/t1/channels/c1/messages?$skiptoken=p2"}
        transport = StubTransport(token_response(), response(200, page1))
        client = make_client(transport=transport)

        ids = [m["id"] for m in client.iter_channel_messages("t1", "c1", limit=1)]

        self.assertEqual(ids, ["m1"])
        self.assertEqual(len(transport.calls), 2, "must not fetch page 2 when done")

    def test_next_link_to_a_foreign_host_is_refused(self):
        page = {"value": [], "@odata.nextLink": "https://evil.example.com/steal"}
        transport = StubTransport(token_response(), response(200, page))
        client = make_client(transport=transport)

        with self.assertRaises(GraphError) as caught:
            list(client.iter_channel_messages("t1", "c1"))
        self.assertIn("unexpected host", str(caught.exception))


# ----------------------------------------------------------------------
# retries: 429 Retry-After, bounded 5xx
# ----------------------------------------------------------------------
class RetryTests(unittest.TestCase):
    def test_429_honours_the_retry_after_header(self):
        sleeper = FakeSleep()
        transport = StubTransport(token_response(),
                                  response(429, body=b"", headers={"Retry-After": "2"}),
                                  response(200, {"value": [{"id": "m1"}]}))
        client = make_client(transport=transport, sleeper=sleeper)

        ids = [m["id"] for m in client.iter_channel_messages("t1", "c1")]

        self.assertEqual(ids, ["m1"])
        self.assertEqual(sleeper.delays, [2.0])
        self.assertEqual(len(transport.calls), 3)

    def test_429_without_retry_after_falls_back_to_backoff(self):
        sleeper = FakeSleep()
        transport = StubTransport(token_response(), response(429),
                                  response(200, {"value": []}))
        client = make_client(transport=transport, sleeper=sleeper)

        client.iter_channel_messages("t1", "c1").__next__() if False else list(
            client.iter_channel_messages("t1", "c1"))

        self.assertEqual(sleeper.delays, [1.0])

    def test_5xx_is_retried_then_succeeds(self):
        sleeper = FakeSleep()
        transport = StubTransport(token_response(), response(502),
                                  response(200, {"value": [{"id": "m1"}]}))
        client = make_client(transport=transport, sleeper=sleeper)

        ids = [m["id"] for m in client.iter_channel_messages("t1", "c1")]

        self.assertEqual(ids, ["m1"])
        self.assertEqual(sleeper.delays, [1.0])

    def test_5xx_retries_are_bounded(self):
        sleeper = FakeSleep()
        transport = StubTransport(
            token_response(),
            *[response(503)] * (graph.MAX_RETRIES + 1),
        )
        client = make_client(transport=transport, sleeper=sleeper)

        with self.assertRaises(GraphError) as caught:
            list(client.iter_channel_messages("t1", "c1"))

        self.assertIn("HTTP 503", str(caught.exception))
        self.assertEqual(len(transport.calls), 1 + graph.MAX_RETRIES + 1)
        self.assertEqual(sleeper.delays, [1.0, 2.0, 4.0])

    def test_4xx_other_than_429_is_not_retried(self):
        transport = StubTransport(token_response(), response(403, {
            "error": {"code": "accessDenied", "message": "Insufficient privileges."}}))
        client = make_client(transport=transport)

        with self.assertRaises(GraphError) as caught:
            list(client.iter_channel_messages("t1", "c1"))
        self.assertIn("accessDenied", str(caught.exception))
        self.assertEqual(len(transport.calls), 2, "no retry for 403")


# ----------------------------------------------------------------------
# missing credentials
# ----------------------------------------------------------------------
class CredentialTests(unittest.TestCase):
    def test_missing_credentials_name_every_variable_and_the_fix(self):
        with self.assertRaises(MissingCredentials) as caught:
            GraphCredentials.from_env({})
        message = str(caught.exception)
        for name in CRED_VARS:
            self.assertIn(name, message)
        self.assertIn("Entra ID", message)
        self.assertIn("admin consent", message)
        self.assertEqual(MissingCredentials.exit_code, 2)

    def test_only_the_missing_credentials_are_reported(self):
        env = dict(FAKE_ENV)
        env.pop("MSGRAPH_CLIENT_SECRET")
        with self.assertRaises(MissingCredentials) as caught:
            GraphCredentials.from_env(env)
        listed = str(caught.exception).split(". Register")[0]
        self.assertIn("MSGRAPH_CLIENT_SECRET", listed)
        self.assertNotIn("MSGRAPH_TENANT_ID", listed)

    def test_credentials_come_from_env_by_default(self):
        with graph_env():
            credentials = GraphCredentials.from_env()
        self.assertEqual(credentials.tenant_id, TENANT)
        self.assertEqual(credentials.client_id, CLIENT_ID)
        self.assertEqual(credentials.token_url, TOKEN_URL)

    def test_cli_exits_non_zero_with_an_actionable_message(self):
        with env_without_graph_creds():
            stderr, stdout = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                code = main(["--source", "sharepoint", "--site", "root", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("MSGRAPH_TENANT_ID", stderr.getvalue())
        self.assertIn("Entra ID", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "", "nothing should be indexed")

    def test_cli_requires_a_drive_or_team_for_those_sources(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--source", "onedrive", "--dry-run"]), 2)
            self.assertEqual(main(["--source", "teams", "--dry-run"]), 2)
        self.assertIn("--drive", stderr.getvalue())
        self.assertIn("--team", stderr.getvalue())


# ----------------------------------------------------------------------
# mapping: Graph entity -> archive record
# ----------------------------------------------------------------------
class MappingTests(unittest.TestCase):
    def test_drive_item_maps_to_a_document_record(self):
        item = {
            "id": "file-1",
            "name": "Q3 report.md",
            "size": 1200,
            "lastModifiedDateTime": "2026-03-04T05:06:07Z",
            "file": {"mimeType": "text/markdown"},
            "parentReference": {"path": "/drives/b!xyz/root:/Shared Documents/Reports"},
        }
        record = mapped(map_drive_item(item, text="# Q3\nSite deploy went live.",
                                      source="sharepoint:Contoso Legal",
                                      path="Shared Documents/Reports"))

        self.assertEqual(set(record), {"source", "text", "kind", "created_at"})
        self.assertEqual(record["source"], "sharepoint:Contoso Legal")
        self.assertEqual(record["kind"], "document")
        self.assertEqual(record["created_at"], "2026-03-04T05:06:07Z")
        self.assertIn("Q3 report.md", record["text"])
        self.assertIn("Shared Documents/Reports", record["text"])
        self.assertIn("Site deploy went live.", record["text"])
        # raw Graph JSON must not leak into the index
        self.assertNotIn("parentReference", record["text"])
        self.assertNotIn("b!xyz", record["text"])
        self.assertNotIn("odata", record["text"])

    def test_folder_path_is_derived_from_parent_reference(self):
        item = {"name": "a.txt",
                "parentReference": {"path": "/drives/b!x/root:/Sites/Ops/Docs"}}
        self.assertEqual(folder_path(item), "Sites/Ops/Docs")
        self.assertEqual(folder_path({}), "")

    def test_drive_item_without_indexable_text_is_skipped(self):
        self.assertIsNone(map_drive_item({"name": "setup.exe", "file": {}}, text="MZ"))
        self.assertIsNone(map_drive_item({"name": "empty.txt", "file": {}}, text="   \n"))
        self.assertIsNone(map_drive_item({"file": {}}, text="orphan"))
        self.assertTrue(is_indexable("contract.docx"))
        self.assertTrue(is_indexable("notes.MD"))
        self.assertFalse(is_indexable("photo.png"))
        self.assertFalse(is_indexable(""))

    def test_teams_message_maps_with_html_stripped(self):
        message = {
            "id": "m1",
            "createdDateTime": "2026-02-01T09:30:00Z",
            "subject": "Deployment",
            "from": {"user": {"displayName": "Aisha Khan"}},
            "body": {"contentType": "html",
                     "content": "<p>Deploy is <b>live</b> on the site.</p>"},
        }
        replies = [{"from": {"user": {"displayName": "Omar"}},
                    "body": {"contentType": "text", "content": "Thanks, checking now"}}]

        record = mapped(map_teams_message(message, source="teams:Ops#General",
                                         replies=replies))

        self.assertEqual(set(record), {"source", "text", "kind", "created_at"})
        self.assertEqual(record["source"], "teams:Ops#General")
        self.assertEqual(record["kind"], "chat")
        self.assertEqual(record["created_at"], "2026-02-01T09:30:00Z")
        self.assertIn("Subject: Deployment", record["text"])
        self.assertIn("Aisha Khan: Deploy is live on the site.", record["text"])
        self.assertIn("Omar: Thanks, checking now", record["text"])
        self.assertNotIn("<p>", record["text"])
        self.assertNotIn("contentType", record["text"])

    def test_teams_message_without_body_is_skipped(self):
        self.assertIsNone(map_teams_message(
            {"id": "m2", "body": {"contentType": "html", "content": "<p></p>"}}))
        self.assertIsNone(map_teams_message({}))
        # a reply alone still carries searchable text
        self.assertIsNotNone(map_teams_message(
            {"id": "m3", "body": {}},
            replies=[{"body": {"contentType": "text", "content": "seed reply"}}]))

    def test_arabic_teams_message_is_preserved(self):
        message = {"id": "m4", "createdDateTime": "2026-02-02T00:00:00Z",
                   "from": {"user": {"displayName": "أحمد"}},
                   "body": {"contentType": "text",
                            "content": "تم توقيع العقد الجديد"}}
        record = mapped(map_teams_message(message))
        self.assertIn("تم توقيع العقد الجديد", record["text"])
        self.assertIn("أحمد", record["text"])


# ----------------------------------------------------------------------
# text extraction (stdlib only)
# ----------------------------------------------------------------------
class ExtractionTests(unittest.TestCase):
    def test_strip_markup_handles_entities_and_line_breaks(self):
        self.assertEqual(
            strip_markup("<div>Hello&nbsp;<b>team</b><br/>Line two</div>"),
            "Hello team\nLine two",
        )

    def test_docx_text_is_extracted_without_dependencies(self):
        data = make_docx(["Deployment plan", "Phase one: migrate SharePoint."])
        text = extract_text("plan.docx", data)
        self.assertIn("Deployment plan", text)
        self.assertIn("Phase one: migrate SharePoint.", text)
        self.assertNotIn("<w:t>", text)

    def test_unreadable_ooxml_yields_no_text(self):
        self.assertEqual(extract_text("broken.docx", b"not a zip file"), "")
        self.assertEqual(map_drive_item({"name": "broken.docx"}, text=""), None)

    def test_text_files_and_arabic_round_trip(self):
        self.assertEqual(extract_text("note.txt", "خطة النشر".encode("utf-8")),
                         "خطة النشر")


# ----------------------------------------------------------------------
# end-to-end ingestion (mocked transport, real index)
# ----------------------------------------------------------------------
class SharePointIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "archive.db")

    def _archive(self):
        archive = Archive(self.db_path)
        self.addCleanup(archive.close)
        return archive

    @staticmethod
    def _routes():
        return [
            (r"oauth2/v2\.0/token", token_response()),
            (r"/sites/contoso$", response(200, {"id": "contoso",
                                                "displayName": "Contoso Legal"})),
            (r"/sites/contoso/drives$", response(200, {
                "value": [{"id": "drive-1", "name": "Documents"}]})),
            (r"/items/root/children$", response(200, {"value": [
                {"id": "folder-1", "name": "Contracts", "folder": {"childCount": 1}},
                {"id": "file-1", "name": "brief.txt", "size": 34,
                 "file": {"mimeType": "text/plain"},
                 "lastModifiedDateTime": "2026-02-02T00:00:00Z",
                 "parentReference": {"path": "/drives/drive-1/root:/Shared Documents"}},
                {"id": "logo-1", "name": "logo.png", "file": {"mimeType": "image/png"}},
            ]})),
            (r"/items/folder-1/children$", response(200, {"value": [
                {"id": "file-2", "name": "عقد.txt", "file": {"mimeType": "text/plain"},
                 "lastModifiedDateTime": "2026-02-03T00:00:00Z",
                 "parentReference": {
                     "path": "/drives/drive-1/root:/Shared Documents/Contracts"}},
            ]})),
            (r"/items/file-1/content$", response(200, "Site deploy is live in Riyadh.")),
            (r"/items/file-2/content$", response(200, "تم توقيع العقد الجديد مع العميل")),
        ]

    def test_documents_and_arabic_are_indexed_and_searchable(self):
        client = make_client(transport=RoutingTransport(self._routes()))
        archive = self._archive()
        lines = []

        stats = ingest_sharepoint(client, archive, site_id="contoso", out=lines.append)

        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["skipped"], 1)  # logo.png
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(archive.stats()["items"], 2)
        self.assertEqual(list(archive.stats()["sources"]), ["sharepoint:Contoso Legal"])

        hits = archive.search("deploy")  # English
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "document")
        self.assertIn("brief.txt", hits[0]["raw"])
        self.assertIn("Riyadh", hits[0]["raw"])
        self.assertNotIn("parentReference", hits[0]["raw"])

        arabic = archive.search("العقد")  # Arabic (alef/ال- normalized)
        self.assertTrue(arabic, "Arabic document should be searchable")
        self.assertIn("عقد.txt", arabic[0]["raw"])
        self.assertIn("Contracts", arabic[0]["raw"])

    def test_dry_run_lists_items_without_writing_or_downloading(self):
        class ExplodingArchive:
            def add(self, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("dry-run must never touch the indexer")

        transport = RoutingTransport(self._routes())
        client = make_client(transport=transport)
        lines = []

        stats = ingest_sharepoint(client, ExplodingArchive(), site_id="contoso",
                                  dry_run=True, out=lines.append)

        self.assertEqual(stats["planned"], 2)
        self.assertEqual(stats["indexed"], 0)
        listing = [line for line in lines if line.startswith("[dry-run]")]
        self.assertEqual(len(listing), 2)
        self.assertTrue(any("brief.txt" in line for line in listing))
        self.assertFalse([url for url in transport.urls if "/content" in url],
                         "dry-run must not download file content")

    def test_limit_caps_the_run(self):
        client = make_client(transport=RoutingTransport(self._routes()))
        archive = self._archive()
        stats = ingest_sharepoint(client, archive, site_id="contoso", limit=1)
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(archive.stats()["items"], 1)


class TeamsIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "archive.db")

    def _archive(self):
        archive = Archive(self.db_path)
        self.addCleanup(archive.close)
        return archive

    @staticmethod
    def _routes():
        page1 = {
            "value": [
                {"id": "m1", "createdDateTime": "2026-02-01T09:30:00Z",
                 "from": {"user": {"displayName": "Aisha Khan"}},
                 "body": {"contentType": "html",
                          "content": "<p>Deploy is <b>live</b>.</p>"}},
                {"id": "m2", "createdDateTime": "2026-02-01T09:31:00Z",
                 "body": {"contentType": "html", "content": "<p></p>"}},
            ],
            "@odata.nextLink": f"{GRAPH}/teams/t1/channels/c1/messages?$skiptoken=p2",
        }
        page2 = {"value": [
            {"id": "m3", "createdDateTime": "2026-02-01T10:00:00Z",
             "subject": "Budget",
             "from": {"user": {"displayName": "Omar"}},
             "body": {"contentType": "text", "content": "تم توقيع العقد الجديد"}},
        ]}
        return [
            (r"oauth2/v2\.0/token", response(200, {"access_token": "tok-1",
                                                   "expires_in": 3600})),
            # ordered: the skiptoken route must win over the bare messages route
            (r"\$skiptoken=p2", response(200, page2)),
            (r"/teams/t1/channels$", response(200, {
                "value": [{"id": "c1", "displayName": "General"}]})),
            (r"/teams/t1$", response(200, {"id": "t1", "displayName": "Ops"})),
            (r"/channels/c1/messages$", response(200, page1)),
            (r"/messages/m1/replies$", response(200, {"value": [
                {"from": {"user": {"displayName": "Omar"}},
                 "body": {"contentType": "text", "content": "Thanks, checking now"}}]})),
            (r"/messages/m3/replies$", response(200, {"value": []})),
            # m2 has an empty body and is dropped by the mapper, but Graph is
            # still asked for its replies first — answer with an empty page.
            (r"/replies$", response(200, {"value": []})),
        ]

    def test_channel_messages_and_replies_are_indexed(self):
        transport = RoutingTransport(self._routes())
        client = make_client(transport=transport)
        archive = self._archive()

        stats = ingest_teams(client, archive, team_id="t1")

        self.assertEqual(stats["indexed"], 2)   # m1 + m3
        self.assertEqual(stats["skipped"], 1)   # m2 had an empty body
        self.assertEqual(archive.stats()["sources"], {"teams:Ops#General": 2})
        messages = archive.search("checking")
        self.assertTrue(messages, "reply text should be searchable")
        self.assertIn("Thanks, checking now", messages[0]["raw"])
        self.assertEqual(messages[0]["kind"], "chat")
        arabic = archive.search("العقد")
        self.assertTrue(arabic)
        self.assertIn("Budget", arabic[0]["raw"])

    def test_no_replies_flag_avoids_the_replies_endpoint(self):
        transport = RoutingTransport(self._routes())
        client = make_client(transport=transport)
        archive = self._archive()

        ingest_teams(client, archive, team_id="t1", include_replies=False)

        self.assertFalse([url for url in transport.urls if "/replies" in url])

    def test_teams_dry_run_does_not_write(self):
        class ExplodingArchive:
            def add(self, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("dry-run must never touch the indexer")

        client = make_client(transport=RoutingTransport(self._routes()))
        stats = ingest_teams(client, ExplodingArchive(), team_id="t1", dry_run=True)
        self.assertEqual(stats["planned"], 3)
        self.assertEqual(stats["indexed"], 0)


# ----------------------------------------------------------------------
# CLI wiring (still fully mocked)
# ----------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "archive.db")

    def test_cli_sharepoint_run_writes_searchable_items(self):
        transport = RoutingTransport(SharePointIngestTests._routes())
        stdout = io.StringIO()
        with graph_env(), patched_ingest_client(transport), \
                contextlib.redirect_stdout(stdout):
            code = main(["--db", self.db_path, "--source", "sharepoint",
                         "--site", "contoso"])

        self.assertEqual(code, 0)
        self.assertIn("2 item(s) indexed", stdout.getvalue())
        archive = Archive(self.db_path)
        self.addCleanup(archive.close)
        self.assertEqual(archive.stats()["items"], 2)
        self.assertTrue(archive.search("Riyadh"))

    def test_cli_dry_run_touches_no_database(self):
        never = os.path.join(self.tmp.name, "never.db")
        transport = RoutingTransport(SharePointIngestTests._routes())
        stdout = io.StringIO()
        with graph_env(), patched_ingest_client(transport), \
                contextlib.redirect_stdout(stdout):
            code = main(["--db", never, "--source", "sharepoint",
                         "--site", "contoso", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(never), "dry-run must not create the db")
        self.assertIn("2 item(s) would be indexed", stdout.getvalue())
        self.assertIn("[dry-run: nothing written]", stdout.getvalue())

    def test_cli_surfaces_graph_errors_with_exit_code_3(self):
        transport = RoutingTransport([
            (r"oauth2/v2\.0/token", token_response()),
            (r"/sites/contoso$", response(403, {"error": {
                "code": "accessDenied",
                "message": "Insufficient privileges to complete the operation."}})),
        ])
        stderr = io.StringIO()
        with graph_env(), patched_ingest_client(transport), \
                contextlib.redirect_stderr(stderr):
            code = main(["--db", os.path.join(self.tmp.name, "x.db"),
                         "--source", "sharepoint", "--site", "contoso"])

        self.assertEqual(code, 3)
        self.assertIn("accessDenied", stderr.getvalue())

    def test_cli_ingest_subcommand_is_registered_on_the_main_parser(self):
        parser = ingest.build_parser()
        args = parser.parse_args(["--source", "teams", "--team", "t1",
                                  "--channel", "c1"])
        self.assertEqual((args.source, args.team, args.channel), ("teams", "t1", "c1"))
        self.assertTrue(args.include_replies)
        self.assertFalse(args.dry_run)

        from archivepilot.cli import build_parser
        subcommand = build_parser().parse_args(["ingest", "--source", "sharepoint",
                                                "--site", "contoso", "--dry-run"])
        self.assertEqual(subcommand.cmd, "ingest")
        self.assertEqual(subcommand.source, "sharepoint")
        self.assertTrue(subcommand.dry_run)
        self.assertTrue(callable(subcommand.fn))


if __name__ == "__main__":
    unittest.main()
