"""Graph-native SharePoint page discovery guidance and insertion query validation."""

import re

PAGE = "/sites/{site}/pages/{page}/microsoft.graph.sitePage"
PAGE_WEBPART_COLLECTIONS = (
    PAGE + "/canvasLayout/verticalSection/webparts",
    PAGE + "/canvasLayout/horizontalSections/{section}/columns/{column}/webparts",
)

_WEBPART_INSERT = re.compile(
    r"/sites/[^/]+/pages/[^/]+/microsoft\.graph\.sitePage/canvasLayout/"
    r"(?:verticalSection|horizontalSections/[^/]+/columns/[^/]+)/webparts"
)


def validate_page_query(method, path, query):
    """Keep the page insertion index from becoming a general Graph query escape."""
    if "index" not in query:
        return
    if method != "POST" or not _WEBPART_INSERT.fullmatch(path):
        raise ValueError("index is supported only when creating a SharePoint page web part.")
    value = query["index"]
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("Web part index must be a nonnegative decimal integer string.")


# Authoring allowlist documented for Graph v1.0; discovery guidance only. Graph
# remains the validator so new supported payload fields do not require a release.
SUPPORTED_STANDARD_WEBPARTS = {
    "Bing Maps": "e377ea37-9047-43b9-8cdb-a761be2f8e09",
    "Button": "0f087d7f-520e-42b7-89c0-496aaf979d58",
    "Call To Action": "df8e44e7-edd5-46d5-90da-aca1539313b8",
    "Divider": "2161a1c6-db61-4731-b97c-3cdb303f7cbb",
    "Document Embed": "b7dd04e1-19ce-4b24-9132-b60a1c2b910d",
    "Image": "d1d91016-032f-456d-98a4-721247c305e8",
    "Image Gallery": "af8be689-990e-492a-81f7-ba3e4cd3ed9c",
    "Link Preview": "6410b3b6-d440-4663-8744-378976dc041e",
    "Org Chart": "e84a8ca2-f63c-4fb9-bc0b-d8eef5ccb22b",
    "People": "7f718435-ee4d-431c-bdbf-9c4ff326f46e",
    "Quick Links": "c70391ea-0b10-4ee9-b2b4-006d3fcad0cd",
    "Spacer": "8654b779-4886-46d4-8ffb-b5ed960ee986",
    "Youtube Embed": "544dd15b-cf3c-441b-96da-004d5a8cea1d",
    "Title Area": "cbe7b0a9-3504-44dd-a3a3-0e5cacd07788",
}

PAGE_GUIDANCE = {
    "workflow": (
        "Resolve the site ID, reconcile the repository manifest with existing page IDs/names, "
        "upload assets, write a draft, inspect it in an authenticated browser, publish when "
        "requested, and read back publishingState and webUrl. Save IDs and the published "
        "repository revision in the content repository. This server does not run the Markdown "
        "compiler, own that manifest, or coordinate the browser/GitHub MCPs."
    ),
    "authoring": [
        "Compile Markdown to HTML in textWebPart.innerHtml inside a JSON body. Include "
        "@odata.type #microsoft.graph.sitePage for page creation/updates and the matching "
        "textWebPart or standardWebPart type for web part writes.",
        "Read the typed page with query {'$expand': 'canvasLayout'} before updating content. "
        "Supplying canvasLayout replaces the complete layout; preserve all managed sections "
        "and web parts, or use a targeted section/web part operation.",
        "Build writes from writable properties; remove response-only fields and OData metadata "
        "while retaining the required @odata.type. Do not round-trip an entire GET response.",
        "Use returned section/column IDs. Column widths use a 12-unit grid. Columns can be "
        "listed/read; change section layout through section or complete page updates. Only "
        "one vertical section is allowed, created with PUT (409 if already present).",
        "Page-level webParts/{webpart} uses a web part ID. Nested column/vertical-section "
        "webparts/{index} uses its position; read current positions before edits. POST to a "
        "nested webparts collection accepts optional query {'index': '0'} for insertion.",
        "Use textWebPart for formatted text; standardWebPart needs its supported webPartType "
        "and type-specific data. Hero, News rollups, and custom SPFx authoring are not in the "
        "documented standard web part list. Inspect unfamiliar existing parts before editing.",
        "promotionKind can promote a page to newsPost; Graph does not support demotion. "
        "pageLayout home is a layout, not an instruction to assign the site's homepage.",
        "Publish sends no request body. A 204 confirms the action, but an approval flow can "
        "delay publication. Verify publishingState.level/versionId and the live page before "
        "reporting success; do not assume a draft is public.",
        "After write_outcome_unknown, inspect the existing page or list pages by name before "
        "retrying. Creation is not an upsert; repeated POST calls can create duplicates. "
        "Respect conflicts/checkouts and reconcile concurrent changes before overwriting.",
    ],
    "supported_standard_webparts": SUPPORTED_STANDARD_WEBPARTS,
    "examples": {
        "resolve_site": {
            "method": "GET",
            "path": "/sites/contoso.sharepoint.com:/sites/AIHub",
        },
        "find_existing_page": {
            "method": "GET",
            "path": "/sites/{site}/pages/microsoft.graph.sitePage",
            "query": {
                "$filter": "name eq 'getting-started.aspx'",
                "$select": "id,name,title,webUrl,publishingState",
            },
        },
        "read_layout": {
            "method": "GET",
            "path": PAGE,
            "query": {"$expand": "canvasLayout"},
        },
        "create_draft": {
            "method": "POST",
            "path": "/sites/{site}/pages",
            "body": {
                "@odata.type": "#microsoft.graph.sitePage",
                "name": "getting-started.aspx",
                "title": "Getting started",
                "pageLayout": "article",
                "promotionKind": "page",
                "showComments": False,
                "canvasLayout": {
                    "horizontalSections": [
                        {
                            "id": "1",
                            "layout": "oneColumn",
                            "emphasis": "none",
                            "columns": [
                                {
                                    "id": "1",
                                    "width": 12,
                                    "webparts": [
                                        {
                                            "@odata.type": "#microsoft.graph.textWebPart",
                                            "id": "894734bf-458d-438c-b5ab-fb48c5d7ec26",
                                            "innerHtml": (
                                                "<h2>Welcome</h2>"
                                                "<p>Start with the AI Hub guides.</p>"
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            },
        },
        "update_text": {
            "method": "PATCH",
            "path": PAGE + "/webParts/{webpart}",
            "body": {
                "@odata.type": "#microsoft.graph.textWebPart",
                "innerHtml": "<h2>Welcome</h2><p>The guide has been updated.</p>",
            },
        },
        "publish": {"method": "POST", "path": PAGE + "/publish"},
        "verify_publication": {
            "method": "GET",
            "path": PAGE,
            "query": {"$select": "id,name,webUrl,publishingState"},
        },
    },
    "references": [
        "https://learn.microsoft.com/en-us/graph/api/site-getbypath?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/sitepage-create?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/sitepage-update?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/sitepage-get?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/sitepage-publish?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/sitepage-create-webpart?view=graph-rest-1.0",
        "https://learn.microsoft.com/en-us/graph/api/resources/horizontalsectioncolumn?view=graph-rest-1.0",
    ],
}
