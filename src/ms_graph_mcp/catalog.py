"""Curated Graph v1.0 routes and read/write classification, independent of token permissions."""

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from urllib.parse import unquote


@lru_cache(maxsize=2048)
def route_pattern(template):
    return re.compile(re.sub(r"\\\{[^{}]+\\\}", r"[^/]+", re.escape(template)))


@dataclass(frozen=True)
class Operation:
    service: str
    path: str
    method: str
    read_only: bool
    permission_guidance: str

    def matches(self, path):
        return route_pattern(self.path).fullmatch(path) is not None


OPERATIONS: list[Operation] = []


def add(service, path, methods, permission, read_posts=()):
    for method in methods.split():
        OPERATIONS.append(
            Operation(service, path, method, method == "GET" or method in read_posts, permission)
        )


def collection(service, path, permission, *, create=True, edit=True):
    add(service, path, "GET POST" if create else "GET", permission)
    add(service, path + "/{id}", "GET PATCH DELETE" if edit else "GET", permission)


add("profile", "/me", "GET PATCH", "User.Read; User.ReadWrite for edits")
add(
    "directory",
    "/users",
    "GET",
    "User.ReadBasic.All (basic fields); broader fields need additional consent",
)
add("directory", "/users/{user}", "GET", "User.ReadBasic.All (basic fields)")
for owner in ("/me", "/users/{user}"):
    add("people", owner + "/people", "GET", "People.Read")
    mail = (
        "Mail.ReadWrite; Mail.ReadWrite.Shared for shared mailboxes; "
        "Mail.Send/Shared for send actions"
    )
    collection("mail", owner + "/messages", mail)
    collection("mail", owner + "/mailFolders", mail)
    collection("mail", owner + "/mailFolders/{folder}/childFolders", mail)
    collection("mail", owner + "/mailFolders/{folder}/messages", mail)
    add("mail", owner + "/sendMail", "POST", mail)
    for suffix in (
        "send",
        "reply",
        "replyAll",
        "forward",
        "createReply",
        "createReplyAll",
        "createForward",
        "copy",
        "move",
    ):
        add("mail", owner + "/messages/{message}/" + suffix, "POST", mail)
    add("mail", owner + "/messages/{message}/$value", "GET", mail)
    collection("mail", owner + "/messages/{message}/attachments", mail, edit=False)
    add("mail", owner + "/messages/{message}/attachments/{attachment}", "DELETE", mail)
    add("mail", owner + "/messages/{message}/attachments/{attachment}/$value", "GET", mail)
    cal = "Calendars.ReadWrite; Calendars.ReadWrite.Shared for shared calendars"
    for path in (
        "/calendars",
        "/events",
        "/calendars/{calendar}/events",
        "/calendarGroups",
        "/calendarGroups/{group}/calendars",
    ):
        collection("calendar", owner + path, cal)
    for path in (
        "/calendar",
        "/calendarView",
        "/calendars/{calendar}/calendarView",
        "/events/{event}/instances",
    ):
        add("calendar", owner + path, "GET", cal)
    for action in (
        "accept",
        "tentativelyAccept",
        "decline",
        "cancel",
        "dismissReminder",
        "snoozeReminder",
    ):
        add("calendar", owner + "/events/{event}/" + action, "POST", cal)
    for action in ("getSchedule",):
        add("calendar", owner + "/calendar/" + action, "POST", cal, read_posts=("POST",))
    add("calendar", owner + "/findMeetingTimes", "POST", cal, read_posts=("POST",))
    for path in (
        "/contacts",
        "/contactFolders",
        "/contactFolders/{folder}/contacts",
        "/contactFolders/{folder}/childFolders",
    ):
        collection(
            "contacts",
            owner + path,
            "Contacts.ReadWrite; Contacts.ReadWrite.Shared for shared contacts",
        )
    add("outlook", owner + "/mailboxSettings", "GET PATCH", "MailboxSettings.ReadWrite")
    collection("outlook", owner + "/outlook/masterCategories", "MailboxSettings.ReadWrite")
    collection("outlook", owner + "/mailFolders/inbox/messageRules", "MailboxSettings.ReadWrite")
    add("outlook", owner + "/outlook/supportedTimeZones", "GET", "MailboxSettings.Read")
    for path in (
        "/todo/lists",
        "/todo/lists/{list}/tasks",
        "/todo/lists/{list}/tasks/{task}/checklistItems",
        "/todo/lists/{list}/tasks/{task}/linkedResources",
    ):
        collection("todo", owner + path, "Tasks.ReadWrite")
    add("planner", owner + "/planner/tasks", "GET", "Tasks.Read")
    add("teams", owner + "/joinedTeams", "GET", "Team.ReadBasic.All")
    add("chats", owner + "/chats", "GET", "Chat.ReadWrite")
    collection("meetings", owner + "/onlineMeetings", "OnlineMeetings.ReadWrite")
    for artifact, scope in (
        ("transcripts", "OnlineMeetingTranscript.Read.All"),
        ("recordings", "OnlineMeetingRecording.Read.All"),
    ):
        path = owner + "/onlineMeetings/{meeting}/" + artifact
        collection("meetings", path, scope, create=False, edit=False)
        add("meetings", path + "/{artifact}/content", "GET", scope)
    for drive in (owner + "/drive",):
        add("files", drive, "GET", "Files.ReadWrite.All")
    add("files", owner + "/drives", "GET", "Files.ReadWrite.All")

add("teams", "/teams/{team}", "GET", "Team.ReadBasic.All")
collection("teams", "/teams/{team}/channels", "Channel.ReadBasic.All", create=False, edit=False)
for root in ("/teams/{team}/channels/{channel}/messages", "/chats/{chat}/messages"):
    scope = (
        "ChannelMessage.Read.All, ChannelMessage.Send; edits require ChannelMessage.ReadWrite"
        if root.startswith("/teams")
        else "Chat.ReadWrite, ChatMessage.Send"
    )
    collection("teams" if root.startswith("/teams") else "chats", root, scope, edit=False)
    add("teams", root + "/{message}", "PATCH", scope)
    for action in ("softDelete", "undoSoftDelete", "setReaction", "unsetReaction"):
        add("teams", root + "/{message}/" + action, "POST", scope)
collection(
    "teams",
    "/teams/{team}/channels/{channel}/messages/{message}/replies",
    "ChannelMessage.Read.All, ChannelMessage.Send",
    edit=False,
)
add("teams", "/teams/{team}/channels/{channel}/filesFolder", "GET", "Files.Read.All")
collection("chats", "/chats", "Chat.ReadWrite, Chat.Create", edit=False)
for root in ("/planner/plans", "/planner/buckets", "/planner/tasks"):
    add("planner", root, "POST", "Tasks.ReadWrite")
    add("planner", root + "/{id}", "GET PATCH DELETE", "Tasks.ReadWrite")
for path in (
    "/planner/plans/{plan}/tasks",
    "/planner/plans/{plan}/buckets",
    "/planner/buckets/{bucket}/tasks",
    "/groups/{group}/planner/plans",
):
    add("planner", path, "GET", "Tasks.Read; group discovery can require Group.Read.All")
for path in ("/planner/plans/{plan}/details", "/planner/tasks/{task}/details"):
    add("planner", path, "GET PATCH", "Tasks.ReadWrite; If-Match required for updates")
add("directory", "/groups", "GET", "Group.Read.All")
add("directory", "/groups/{group}", "GET", "Group.Read.All")
add("sites", "/sites", "GET", "Sites.Read.All")
add("sites", "/sites/{site}", "GET", "Sites.Read.All")
add("sites", "/sites/{site}/sites", "GET", "Sites.Read.All")
add("sites", "/sites/{site}/drives", "GET", "Files.ReadWrite.All")
for path in ("/sites/{site}/lists", "/sites/{site}/lists/{list}/items", "/sites/{site}/pages"):
    collection("sites", path, "Sites.ReadWrite.All")
add("sites", "/sites/{site}/lists/{list}/items/{item}/fields", "GET PATCH", "Sites.ReadWrite.All")
add(
    "sites",
    "/sites/{site}/pages/{page}/microsoft.graph.sitePage",
    "GET PATCH",
    "Sites.ReadWrite.All",
)
add(
    "sites",
    "/sites/{site}/pages/{page}/microsoft.graph.sitePage/publish",
    "POST",
    "Sites.ReadWrite.All",
)

for drive in (
    "/me/drive",
    "/users/{user}/drive",
    "/drives/{drive}",
    "/sites/{site}/drive",
    "/groups/{group}/drive",
):
    add("files", drive, "GET", "Files.ReadWrite.All")
    for item in (drive + "/root", drive + "/items/{item}"):
        add("files", item, "GET PATCH DELETE", "Files.ReadWrite.All")
        for child in ("children", "versions", "permissions", "thumbnails", "delta"):
            add("files", item + "/" + child, "GET", "Files.ReadWrite.All")
        add("files", item + "/children", "POST", "Files.ReadWrite.All")
        for action in (
            "copy",
            "createLink",
            "invite",
            "createUploadSession",
            "checkout",
            "checkin",
        ):
            add("files", item + "/" + action, "POST", "Files.ReadWrite.All")
        add("files", item + "/permissions/{permission}", "GET PATCH DELETE", "Files.ReadWrite.All")
        add("files", item + "/versions/{version}/restoreVersion", "POST", "Files.ReadWrite.All")
        add("files", item + "/search(q='{query}')", "GET", "Files.Read.All")
    add(
        "files",
        drive + "/items/{parent}:/{filename}:/createUploadSession",
        "POST",
        "Files.ReadWrite.All",
    )
    add("files", drive + "/root:/{filename}:/createUploadSession", "POST", "Files.ReadWrite.All")
    workbook = drive + "/items/{item}/workbook"
    add("excel", workbook, "GET", "Files.ReadWrite")
    for action in ("createSession", "closeSession", "refreshSession"):
        add("excel", workbook + "/" + action, "POST", "Files.ReadWrite")
    add("excel", workbook + "/application/calculate", "POST", "Files.ReadWrite")
    for path in (
        "/worksheets",
        "/tables",
        "/names",
        "/worksheets/{sheet}/tables",
        "/worksheets/{sheet}/charts",
    ):
        collection("excel", workbook + path, "Files.ReadWrite", create=False)
        add("excel", workbook + path + "/add", "POST", "Files.ReadWrite")
    for path in (
        "/worksheets/{sheet}/range(address='{address}')",
        "/worksheets/{sheet}/usedRange",
        "/worksheets/{sheet}/usedRange(valuesOnly=true)",
        "/tables/{table}/range",
    ):
        add("excel", workbook + path, "GET PATCH", "Files.ReadWrite")
        for action in ("clear", "insert", "delete"):
            add("excel", workbook + path + "/" + action, "POST", "Files.ReadWrite")
    for path in ("/tables/{table}/rows", "/tables/{table}/columns"):
        add("excel", workbook + path, "GET", "Files.ReadWrite")
        add("excel", workbook + path + "/add", "POST", "Files.ReadWrite")
        add("excel", workbook + path + "/{index}", "GET PATCH DELETE", "Files.ReadWrite")
    add(
        "excel",
        workbook + "/worksheets/{sheet}/charts/{chart}/image(width=0,height=0,fittingMode='fit')",
        "GET",
        "Files.ReadWrite",
    )
add("files", "/shares/{share}/driveItem", "GET", "Files.ReadWrite.All")
add(
    "search",
    "/search/query",
    "POST",
    "Entity-specific delegated permissions; see Microsoft Search documentation",
    read_posts=("POST",),
)
collection(
    "copilot",
    "/copilot/users/{user}/onlineMeetings/{meeting}/aiInsights",
    "OnlineMeetingAiInsight.Read.All; eligible user/Copilot license required",
    create=False,
    edit=False,
)

# Deduplicate overlapping owner/drive templates while preserving discovery order.
OPERATIONS = list({(o.path, o.method): o for o in OPERATIONS}.values())


def canonical_path(path: str) -> str:
    if not path.startswith("/") or path.startswith("//") or len(path) > 4096:
        raise ValueError("Use a Graph-relative path beginning with one slash.")
    decoded = unquote(path, errors="strict")
    if any(c in decoded for c in ("\\", "?", "#", "%")) or any(ord(c) < 32 for c in decoded):
        raise ValueError(
            "Queries belong in query; encoded delimiters and double encoding are rejected."
        )
    if any(part in ("", ".", "..") for part in decoded[1:].split("/")):
        raise ValueError("Empty or traversal path segments are not allowed.")
    return decoded


def resolve(method, path, read_only):
    path = canonical_path(path)
    matches = [o for o in OPERATIONS if o.method == method and o.matches(path)]
    if not matches or not any(o.read_only == read_only for o in matches):
        raise ValueError(
            "Operation is not in this tool's catalog. Use graph_describe for supported routes."
        )
    return path


def describe(service=None, query=None, path=None, method=None, offset=0, limit=20):
    matches = [
        o
        for o in OPERATIONS
        if (not service or o.service == service)
        and (not query or query.lower() in (o.path + " " + o.service).lower())
        and (not path or o.matches(canonical_path(path)))
        and (not method or o.method == method)
    ]
    return {
        "operations": [asdict(o) for o in matches[offset : offset + limit]],
        "total": len(matches),
        "next_offset": offset + limit if offset + limit < len(matches) else None,
        "api_version": "v1.0",
        "availability": "supported_unverified",
        "notes": (
            "Permission guidance is not a grant. Graph enforces delegated "
            "consent and user rights. Admin, beta and batch routes are excluded."
        ),
    }
