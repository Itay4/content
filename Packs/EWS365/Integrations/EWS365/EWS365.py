import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *
import sys
import traceback
import json
import os
import hashlib
from datetime import timedelta
from io import StringIO
import logging
import warnings
import email
from requests.exceptions import ConnectionError
from collections import deque

from multiprocessing import Process
import exchangelib
from exchangelib.errors import (
    ErrorItemNotFound,
    ResponseMessageError,
    TransportError,
    RateLimitError,
    ErrorInvalidIdMalformed,
    ErrorFolderNotFound,
    ErrorMailboxStoreUnavailable,
    ErrorMailboxMoveInProgress,
    ErrorNameResolutionNoResults,
    ErrorInvalidPropertyRequest,
    ErrorIrresolvableConflict,
    MalformedResponseError,
)
from exchangelib.items import Item, Message, Contact
from exchangelib.services.common import EWSService, EWSAccountService
from exchangelib.util import create_element, add_xml_child, MNS, TNS
from exchangelib import (
    IMPERSONATION,
    DELEGATE,
    Account,
    Credentials,
    EWSDateTime,
    EWSTimeZone,
    Configuration,
    BASIC,
    FileAttachment,
    Version,
    Folder,
    HTMLBody,
    Body,
    ItemAttachment,
)
from exchangelib.version import EXCHANGE_O365
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

# Ignore warnings print to stdout
warnings.filterwarnings("ignore")

""" Constants """

# move results
MOVED_TO_MAILBOX = "movedToMailbox"
MOVED_TO_FOLDER = "movedToFolder"

# item types
FILE_ATTACHMENT_TYPE = "FileAttachment"
ITEM_ATTACHMENT_TYPE = "ItemAttachment"
ATTACHMENT_TYPE = "attachmentType"

TOIS_PATH = "/root/Top of Information Store/"

# context keys
ATTACHMENT_ID = "attachmentId"
ATTACHMENT_ORIGINAL_ITEM_ID = "originalItemId"
NEW_ITEM_ID = "newItemId"
MESSAGE_ID = "messageId"
ITEM_ID = "itemId"
ACTION = "action"
MAILBOX = "mailbox"
MAILBOX_ID = "mailboxId"
FOLDER_ID = "id"

# context paths
CONTEXT_UPDATE_EWS_ITEM = "EWS.Items(val.{0} == obj.{0} || (val.{1} && obj.{1} && val.{1} == obj.{1}))".format(
    ITEM_ID, MESSAGE_ID
)
CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT = "EWS.Items(val.{0} == obj.{1})".format(
    ITEM_ID, ATTACHMENT_ORIGINAL_ITEM_ID
)
CONTEXT_UPDATE_ITEM_ATTACHMENT = ".ItemAttachments(val.{0} == obj.{0})".format(
    ATTACHMENT_ID
)
CONTEXT_UPDATE_FILE_ATTACHMENT = ".FileAttachments(val.{0} == obj.{0})".format(
    ATTACHMENT_ID
)
CONTEXT_UPDATE_FOLDER = "EWS.Folders(val.{0} == obj.{0})".format(FOLDER_ID)

# fetch params
LAST_RUN_TIME = "lastRunTime"
LAST_RUN_IDS = "ids"
LAST_RUN_FOLDER = "folderName"
ERROR_COUNTER = "errorCounter"

# headers
ITEMS_RESULTS_HEADERS = [
    "sender",
    "subject",
    "hasAttachments",
    "datetimeReceived",
    "receivedBy",
    "author",
    "toRecipients",
    "textBody",
]

# Load integration params from demisto
FOLDER_NAME = demisto.params().get("folder", "Inbox")
IS_PUBLIC_FOLDER = demisto.params().get("isPublicFolder", False)
ACCESS_TYPE = (
    IMPERSONATION if demisto.params().get("impersonation", False) else DELEGATE
)
FETCH_ALL_HISTORY = demisto.params().get("fetchAllHistory", False)
IS_TEST_MODULE = False
BaseProtocol.TIMEOUT = int(demisto.params().get("requestTimeout", 120))
MARK_AS_READ = demisto.params().get("markAsRead", False)
MAX_FETCH = min(50, int(demisto.params().get("maxFetch", 50)))
LAST_RUN_IDS_QUEUE_SIZE = 500

# initialized in main()
USERNAME = ""
PASSWORD = ""
config = None
credentials = None

PUBLIC_FOLDERS_ERROR = "Please update your docker image to use public folders"
if IS_PUBLIC_FOLDER and exchangelib.__version__ != "1.12.0":
    if demisto.command() == "test-module":
        demisto.results(PUBLIC_FOLDERS_ERROR)
        exit(3)
    raise Exception(PUBLIC_FOLDERS_ERROR)


class EWSClient:
    def __init__(
        self,
        default_target_mailbox,
        credentials=None,
        folder="Inbox",
        is_public_folder=False,
        impersonation=False,
        fetch_all_history=False,
        requset_timeout="120",
        mark_as_read=False,
        max_fetch="50",
        insecure=True,
        **kwargs
    ):
        BaseProtocol.TIMEOUT = int(requset_timeout)
        self.folder_name = folder
        self.is_public_folder = is_public_folder
        self.access_type = IMPERSONATION if impersonation else DELEGATE
        self.fetch_all_history = fetch_all_history
        self.mark_as_read = mark_as_read
        self.max_fetch = min(50, int(max_fetch))
        self.last_run_ids_queue_size = 500
        if not credentials:
            self.username = ""
            self.password = ""
        elif isinstance(credentials, dict):
            self.username = credentials.get('username')
            self.password = credentials.get('password')
        self.ews_server = "https://outlook.office365.com/EWS/Exchange.asmx/"
        self.account_email = default_target_mailbox
        self.config, self.credentials = prepare(insecure)

    def get_account(self, target_mailbox=None, access_type=ACCESS_TYPE):
        if not target_mailbox:
            target_mailbox = self.account_email
        return Account(
            primary_smtp_address=target_mailbox,
            autodiscover=False,
            config=config,
            access_type=access_type,
        )


# NOTE: Same method used in EWSMailSender
# If you are modifying this probably also need to modify in the other file
def exchangelib_cleanup():
    key_protocols = list(exchangelib.protocol.CachingProtocol._protocol_cache.items())
    try:
        exchangelib.close_connections()
    except Exception as ex:
        demisto.error("Error was found in exchangelib cleanup, ignoring: {}".format(ex))
    for key, protocol in key_protocols:
        try:
            if "thread_pool" in protocol.__dict__:
                demisto.debug(
                    "terminating thread pool key{} id: {}".format(
                        key, id(protocol.thread_pool)
                    )
                )
                protocol.thread_pool.terminate()
                del protocol.__dict__["thread_pool"]
            else:
                demisto.info(
                    "Thread pool not found (ignoring terminate) in protcol dict: {}".format(
                        dir(protocol.__dict__)
                    )
                )
        except Exception as ex:
            demisto.error("Error with thread_pool.terminate, ignoring: {}".format(ex))


""" Prep Functions """


def prepare(insecure):
    handle_proxy()
    if insecure:
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    # todo: replace with OAuth2
    credentials = Credentials(username=USERNAME, password=PASSWORD)
    config_args = {
        "credentials": credentials,
        "auth_type": BASIC,
        "version": Version(EXCHANGE_O365),
        "service_endpoint": "https://outlook.office365.com/EWS/Exchange.asmx",
    }

    return Configuration(**config_args), None




""" LOGGING """

log_stream = None
log_handler = None


def start_logging():
    global log_stream
    global log_handler
    logging.raiseExceptions = False
    if log_stream is None:
        log_stream = StringIO()
        log_handler = logging.StreamHandler(stream=log_stream)
        log_handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
        logger = logging.getLogger()
        logger.addHandler(log_handler)
        logger.setLevel(logging.DEBUG)


def filter_dict_null(d):
    if isinstance(d, dict):
        return dict((k, v) for k, v in list(d.items()) if v is not None)
    return d


def get_attachment_name(attachment_name):
    if attachment_name is None or attachment_name == "":
        return "demisto_untitled_attachment"
    return attachment_name


def get_entry_for_object(title, context_key, obj, headers=None):
    if len(obj) == 0:
        return "There is no output results"
    obj = filter_dict_null(obj)
    if isinstance(obj, list):
        obj = list(map(filter_dict_null, obj))
    if headers and isinstance(obj, dict):
        headers = list(set(headers).intersection(set(obj.keys())))

    return {
        "Type": entryTypes["note"],
        "Contents": obj,
        "ContentsFormat": formats["json"],
        "ReadableContentsFormat": formats["markdown"],
        "HumanReadable": tableToMarkdown(title, obj, headers),
        "EntryContext": {context_key: obj},
    }


def get_items_from_mailbox(account, item_ids):
    if type(item_ids) is not list:
        item_ids = [item_ids]
    items = [Item(id=x) for x in item_ids]
    result = list(account.fetch(ids=items))
    result = [x for x in result if not isinstance(x, ErrorItemNotFound)]
    if len(result) != len(item_ids):
        raise Exception("One or more items were not found. Check the input item ids")
    return result


def get_item_from_mailbox(account, item_id):
    result = get_items_from_mailbox(account, [item_id])
    if len(result) == 0:
        raise Exception("ItemId %s not found" % str(item_id))
    return result[0]


def is_default_folder(folder_path, is_public):
    if is_public is not None:
        return is_public

    if folder_path == FOLDER_NAME:
        return IS_PUBLIC_FOLDER

    return False


def get_folder_by_path(account, path, is_public=False):
    # handle exchange folder id
    if len(path) == 120:
        folders_map = account.root._folders_map
        if path in folders_map:
            return account.root._folders_map[path]

    if is_public:
        folder_result = account.public_folders_root
    elif path == "AllItems":
        folder_result = account.root
    else:
        folder_result = account.inbox.parent  # Top of Information Store
    path = path.replace("/", "\\")
    path = path.split("\\")
    for sub_folder_name in path:
        folder_filter_by_name = [
            x
            for x in folder_result.children
            if x.name.lower() == sub_folder_name.lower()
        ]
        if len(folder_filter_by_name) == 0:
            raise Exception("No such folder %s" % path)
        folder_result = folder_filter_by_name[0]

    return folder_result


class MarkAsJunk(EWSAccountService):
    SERVICE_NAME = "MarkAsJunk"

    def call(self, item_id, move_item):
        elements = list(
            self._get_elements(
                payload=self.get_payload(item_id=item_id, move_item=move_item)
            )
        )
        for element in elements:
            if isinstance(element, ResponseMessageError):
                return element.message
        return "Success"

    def get_payload(self, item_id, move_item):
        junk = create_element(
            "m:%s" % self.SERVICE_NAME,
            IsJunk="true",
            MoveItem="true" if move_item else "false",
        )

        items_list = create_element("m:ItemIds")
        item_element = create_element("t:ItemId", Id=item_id)
        items_list.append(item_element)
        junk.append(items_list)

        return junk


class GetSearchableMailboxes(EWSService):
    SERVICE_NAME = "GetSearchableMailboxes"
    element_container_name = "{%s}SearchableMailboxes" % MNS

    @staticmethod
    def parse_element(element):
        return {
            MAILBOX: element.find("{%s}PrimarySmtpAddress" % TNS).text
            if element.find("{%s}PrimarySmtpAddress" % TNS) is not None
            else None,
            MAILBOX_ID: element.find("{%s}ReferenceId" % TNS).text
            if element.find("{%s}ReferenceId" % TNS) is not None
            else None,
            "displayName": element.find("{%s}DisplayName" % TNS).text
            if element.find("{%s}DisplayName" % TNS) is not None
            else None,
            "isExternal": element.find("{%s}IsExternalMailbox" % TNS).text
            if element.find("{%s}IsExternalMailbox" % TNS) is not None
            else None,
            "externalEmailAddress": element.find("{%s}ExternalEmailAddress" % TNS).text
            if element.find("{%s}ExternalEmailAddress" % TNS) is not None
            else None,
        }

    def call(self):
        elements = self._get_elements(payload=self.get_payload())
        return [self.parse_element(x) for x in elements]

    def get_payload(self):
        element = create_element("m:%s" % self.SERVICE_NAME,)
        return element


class SearchMailboxes(EWSService):
    SERVICE_NAME = "SearchMailboxes"
    element_container_name = "{%s}SearchMailboxesResult/{%s}Items" % (MNS, TNS)

    @staticmethod
    def parse_element(element):
        to_recipients = element.find("{%s}ToRecipients" % TNS)
        if to_recipients:
            to_recipients = [x.text if x is not None else None for x in to_recipients]

        result = {
            ITEM_ID: element.find("{%s}Id" % TNS).attrib["Id"]
            if element.find("{%s}Id" % TNS) is not None
            else None,
            MAILBOX: element.find(
                "{%s}Mailbox/{%s}PrimarySmtpAddress" % (TNS, TNS)
            ).text
            if element.find("{%s}Mailbox/{%s}PrimarySmtpAddress" % (TNS, TNS))
            is not None
            else None,
            "subject": element.find("{%s}Subject" % TNS).text
            if element.find("{%s}Subject" % TNS) is not None
            else None,
            "toRecipients": to_recipients,
            "sender": element.find("{%s}Sender" % TNS).text
            if element.find("{%s}Sender" % TNS) is not None
            else None,
            "hasAttachments": element.find("{%s}HasAttachment" % TNS).text
            if element.find("{%s}HasAttachment" % TNS) is not None
            else None,
            "datetimeSent": element.find("{%s}SentTime" % TNS).text
            if element.find("{%s}SentTime" % TNS) is not None
            else None,
            "datetimeReceived": element.find("{%s}ReceivedTime" % TNS).text
            if element.find("{%s}ReceivedTime" % TNS) is not None
            else None,
        }

        return result

    def call(self, query, mailboxes):
        elements = list(self._get_elements(payload=self.get_payload(query, mailboxes)))
        return [self.parse_element(x) for x in elements]

    def get_payload(self, query, mailboxes):
        def get_mailbox_search_scope(mailbox_id):
            mailbox_search_scope = create_element("t:MailboxSearchScope")
            add_xml_child(mailbox_search_scope, "t:Mailbox", mailbox_id)
            add_xml_child(mailbox_search_scope, "t:SearchScope", "All")
            return mailbox_search_scope

        mailbox_query_element = create_element("t:MailboxQuery")
        add_xml_child(mailbox_query_element, "t:Query", query)
        mailboxes_scopes = []
        for mailbox in mailboxes:
            mailboxes_scopes.append(get_mailbox_search_scope(mailbox))
        add_xml_child(mailbox_query_element, "t:MailboxSearchScopes", mailboxes_scopes)

        element = create_element("m:%s" % self.SERVICE_NAME)
        add_xml_child(element, "m:SearchQueries", mailbox_query_element)
        add_xml_child(element, "m:ResultType", "PreviewOnly")

        return element


class ExpandGroup(EWSService):
    SERVICE_NAME = "ExpandDL"
    element_container_name = "{%s}DLExpansion" % MNS

    @staticmethod
    def parse_element(element):
        return {
            MAILBOX: element.find("{%s}EmailAddress" % TNS).text
            if element.find("{%s}EmailAddress" % TNS) is not None
            else None,
            "displayName": element.find("{%s}Name" % TNS).text
            if element.find("{%s}Name" % TNS) is not None
            else None,
            "mailboxType": element.find("{%s}MailboxType" % TNS).text
            if element.find("{%s}MailboxType" % TNS) is not None
            else None,
        }

    def call(self, email_address, recursive_expansion=False):
        try:
            if recursive_expansion == "True":
                group_members = {}  # type: dict
                self.expand_group_recursive(email_address, group_members)
                return list(group_members.values())
            else:
                return self.expand_group(email_address)
        except ErrorNameResolutionNoResults:
            demisto.results("No results were found.")
            sys.exit()

    def get_payload(self, email_address):
        element = create_element("m:%s" % self.SERVICE_NAME,)
        mailbox_element = create_element("m:Mailbox")
        add_xml_child(mailbox_element, "t:EmailAddress", email_address)
        element.append(mailbox_element)
        return element

    def expand_group(self, email_address):
        elements = self._get_elements(payload=self.get_payload(email_address))
        return [self.parse_element(x) for x in elements]

    def expand_group_recursive(self, email_address, non_dl_emails, dl_emails=set()):
        if email_address in non_dl_emails or email_address in dl_emails:
            return None
        dl_emails.add(email_address)

        for member in self.expand_group(email_address):
            if (
                member["mailboxType"] == "PublicDL"
                or member["mailboxType"] == "PrivateDL"
            ):
                self.expand_group_recursive(member["mailbox"], non_dl_emails, dl_emails)
            else:
                if member["mailbox"] not in non_dl_emails:
                    non_dl_emails[member["mailbox"]] = member


def get_expanded_group(protocol, email_address, recursive_expansion=False):
    group_members = ExpandGroup(protocol=protocol).call(
        email_address, recursive_expansion
    )
    group_details = {"name": email_address, "members": group_members}
    entry_for_object = get_entry_for_object(
        "Expanded group", "EWS.ExpandGroup", group_details
    )
    entry_for_object["HumanReadable"] = tableToMarkdown("Group Members", group_members)
    return entry_for_object


def get_searchable_mailboxes(protocol):
    searchable_mailboxes = GetSearchableMailboxes(protocol=protocol).call()
    return get_entry_for_object(
        "Searchable mailboxes", "EWS.Mailboxes", searchable_mailboxes
    )


def search_mailboxes(
    protocol, filter, limit=100, mailbox_search_scope=None, email_addresses=None
):
    mailbox_ids = []
    limit = int(limit)
    if mailbox_search_scope is not None and email_addresses is not None:
        raise Exception(
            "Use one of the arguments - mailbox-search-scope or email-addresses, not both"
        )
    if email_addresses:
        email_addresses = email_addresses.split(",")
        all_mailboxes = get_searchable_mailboxes(protocol)["EntryContext"][
            "EWS.Mailboxes"
        ]
        for email_address in email_addresses:
            for mailbox in all_mailboxes:
                if (
                    MAILBOX in mailbox
                    and email_address.lower() == mailbox[MAILBOX].lower()
                ):
                    mailbox_ids.append(mailbox[MAILBOX_ID])
        if len(mailbox_ids) == 0:
            raise Exception(
                "No searchable mailboxes were found for the provided email addresses."
            )
    elif mailbox_search_scope:
        mailbox_ids = (
            mailbox_search_scope
            if type(mailbox_search_scope) is list
            else [mailbox_search_scope]
        )
    else:
        entry = get_searchable_mailboxes(protocol)
        mailboxes = [
            x
            for x in entry["EntryContext"]["EWS.Mailboxes"]
            if MAILBOX_ID in list(x.keys())
        ]
        mailbox_ids = [x[MAILBOX_ID] for x in mailboxes]

    try:
        search_results = SearchMailboxes(protocol=protocol).call(filter, mailbox_ids)
        search_results = search_results[:limit]
    except TransportError as e:
        if "ItemCount>0<" in str(e):
            return "No results for search query: " + filter
        else:
            raise e

    return get_entry_for_object(
        "Search mailboxes results", CONTEXT_UPDATE_EWS_ITEM, search_results
    )


def get_last_run():
    last_run = demisto.getLastRun()
    if not last_run or last_run.get(LAST_RUN_FOLDER) != FOLDER_NAME:
        last_run = {LAST_RUN_TIME: None, LAST_RUN_FOLDER: FOLDER_NAME, LAST_RUN_IDS: []}
    if LAST_RUN_TIME in last_run and last_run[LAST_RUN_TIME] is not None:
        last_run[LAST_RUN_TIME] = EWSDateTime.from_string(last_run[LAST_RUN_TIME])

    # In case we have existing last_run data
    if last_run.get(LAST_RUN_IDS) is None:
        last_run[LAST_RUN_IDS] = []

    return last_run


def fetch_last_emails(
    account, folder_name="Inbox", since_datetime=None, exclude_ids=None
):
    qs = get_folder_by_path(account, folder_name, is_public=IS_PUBLIC_FOLDER)
    if since_datetime:
        qs = qs.filter(datetime_received__gte=since_datetime)
    else:
        if not FETCH_ALL_HISTORY:
            last_10_min = EWSDateTime.now(tz=EWSTimeZone.timezone("UTC")) - timedelta(
                minutes=10
            )
            qs = qs.filter(datetime_received__gte=last_10_min)
    qs = qs.filter().only(*[x.name for x in Message.FIELDS])
    qs = qs.filter().order_by("datetime_received")

    result = qs.all()
    result = [x for x in result if isinstance(x, Message)]
    if exclude_ids and len(exclude_ids) > 0:
        exclude_ids = set(exclude_ids)
        result = [x for x in result if x.message_id not in exclude_ids]
    return result


def keys_to_camel_case(value):
    def str_to_camel_case(snake_str):
        components = snake_str.split("_")
        return components[0] + "".join(x.title() for x in components[1:])

    if value is None:
        return None
    if isinstance(value, (list, set)):
        return list(map(keys_to_camel_case, value))
    if isinstance(value, dict):
        return dict(
            (
                keys_to_camel_case(k),
                keys_to_camel_case(v) if isinstance(v, (list, dict)) else v,
            )
            for (k, v) in list(value.items())
        )

    return str_to_camel_case(value)


def email_ec(item):
    return {
        "CC": None
        if not item.cc_recipients
        else [mailbox.email_address for mailbox in item.cc_recipients],
        "BCC": None
        if not item.bcc_recipients
        else [mailbox.email_address for mailbox in item.bcc_recipients],
        "To": None
        if not item.to_recipients
        else [mailbox.email_address for mailbox in item.to_recipients],
        "From": item.author.email_address,
        "Subject": item.subject,
        "Text": item.text_body,
        "HTML": item.body,
        "HeadersMap": {header.name: header.value for header in item.headers},
    }


def parse_item_as_dict(item, email_address, camel_case=False, compact_fields=False):
    def parse_object_as_dict(object):
        raw_dict = {}
        if object is not None:
            for field in object.FIELDS:
                raw_dict[field.name] = getattr(object, field.name, None)
        return raw_dict

    def parse_attachment_as_raw_json(attachment):
        raw_dict = parse_object_as_dict(attachment)
        if raw_dict["attachment_id"]:
            raw_dict["attachment_id"] = parse_object_as_dict(raw_dict["attachment_id"])
        if raw_dict["last_modified_time"]:
            raw_dict["last_modified_time"] = raw_dict["last_modified_time"].ewsformat()
        return raw_dict

    def parse_folder_as_json(folder):
        raw_dict = parse_object_as_dict(folder)
        if "parent_folder_id" in raw_dict:
            raw_dict["parent_folder_id"] = parse_folder_as_json(
                raw_dict["parent_folder_id"]
            )
        if "effective_rights" in raw_dict:
            raw_dict["effective_rights"] = parse_object_as_dict(
                raw_dict["effective_rights"]
            )
        return raw_dict

    raw_dict = {}
    for field, value in list(item.__dict__.items()):
        if type(value) in [str, str, int, float, bool, Body, HTMLBody, None]:
            try:
                if isinstance(value, str):
                    value.encode("utf-8")  # type: ignore
                raw_dict[field] = value
            except Exception:
                pass

    if getattr(item, "attachments", None):
        raw_dict["attachments"] = [
            parse_attachment_as_dict(item.item_id, x) for x in item.attachments
        ]

    for time_field in [
        "datetime_sent",
        "datetime_created",
        "datetime_received",
        "last_modified_time",
        "reminder_due_by",
    ]:
        value = getattr(item, time_field, None)
        if value:
            raw_dict[time_field] = value.ewsformat()

    for dict_field in [
        "effective_rights",
        "parent_folder_id",
        "conversation_id",
        "author",
        "extern_id",
        "received_by",
        "received_representing",
        "reply_to",
        "sender",
        "folder",
    ]:
        value = getattr(item, dict_field, None)
        if value:
            raw_dict[dict_field] = parse_object_as_dict(value)

    for list_dict_field in ["headers", "cc_recipients", "to_recipients"]:
        value = getattr(item, list_dict_field, None)
        if value:
            raw_dict[list_dict_field] = [parse_object_as_dict(x) for x in value]

    if getattr(item, "folder", None):
        raw_dict["folder"] = parse_folder_as_json(item.folder)
        folder_path = (
            item.folder.absolute[len(TOIS_PATH) :]
            if item.folder.absolute.startswith(TOIS_PATH)
            else item.folder.absolute
        )
        raw_dict["folder_path"] = folder_path

    if compact_fields:
        new_dict = {}
        # noinspection PyListCreation
        fields_list = [
            "datetime_created",
            "datetime_received",
            "datetime_sent",
            "sender",
            "has_attachments",
            "importance",
            "message_id",
            "last_modified_time",
            "size",
            "subject",
            "text_body",
            "headers",
            "body",
            "folder_path",
            "is_read",
        ]

        # Docker BC
        # if exchangelib.__version__ == "1.12.0":
        #     if "id" in raw_dict:
        #         new_dict["item_id"] = raw_dict["id"] todo: remove if need be
        # else:
        fields_list.append("item_id")

        for field in fields_list:
            if field in raw_dict:
                new_dict[field] = raw_dict.get(field)
        for field in ["received_by", "author", "sender"]:
            if field in raw_dict:
                new_dict[field] = raw_dict.get(field, {}).get("email_address")
        for field in ["to_recipients"]:
            if field in raw_dict:
                new_dict[field] = [x.get("email_address") for x in raw_dict[field]]
        attachments = raw_dict.get("attachments")
        if attachments and len(attachments) > 0:
            file_attachments = [
                x for x in attachments if x[ATTACHMENT_TYPE] == FILE_ATTACHMENT_TYPE
            ]
            if len(file_attachments) > 0:
                new_dict["FileAttachments"] = file_attachments
            item_attachments = [
                x for x in attachments if x[ATTACHMENT_TYPE] == ITEM_ATTACHMENT_TYPE
            ]
            if len(item_attachments) > 0:
                new_dict["ItemAttachments"] = item_attachments

        raw_dict = new_dict

    if camel_case:
        raw_dict = keys_to_camel_case(raw_dict)

    if email_address:
        raw_dict[MAILBOX] = email_address
    return raw_dict


def parse_incident_from_item(item, is_fetch):
    incident = {}
    labels = []

    try:
        incident["details"] = item.text_body or item.body
    except AttributeError:
        incident["details"] = item.body
    incident["name"] = item.subject
    labels.append({"type": "Email/subject", "value": item.subject})
    incident["occurred"] = item.datetime_created.ewsformat()

    # handle recipients
    if item.to_recipients:
        for recipient in item.to_recipients:
            labels.append({"type": "Email", "value": recipient.email_address})

    # handle cc
    if item.cc_recipients:
        for recipient in item.cc_recipients:
            labels.append({"type": "Email/cc", "value": recipient.email_address})
    # handle email from
    if item.sender:
        labels.append({"type": "Email/from", "value": item.sender.email_address})

    # email format
    email_format = ""
    try:
        if item.text_body:
            labels.append({"type": "Email/text", "value": item.text_body})
            email_format = "text"
    except AttributeError:
        pass
    if item.body:
        labels.append({"type": "Email/html", "value": item.body})
        email_format = "HTML"
    labels.append({"type": "Email/format", "value": email_format})

    # handle attachments
    if item.attachments:
        incident["attachment"] = []
        for attachment in item.attachments:
            file_result = None
            label_attachment_type = None
            label_attachment_id_type = None
            if isinstance(attachment, FileAttachment):
                try:
                    if attachment.content:
                        # file attachment
                        label_attachment_type = "attachments"
                        label_attachment_id_type = "attachmentId"

                        # save the attachment
                        file_name = get_attachment_name(attachment.name)
                        file_result = fileResult(file_name, attachment.content)

                        # check for error
                        if file_result["Type"] == entryTypes["error"]:
                            demisto.error(file_result["Contents"])
                            raise Exception(file_result["Contents"])

                        # save attachment to incident
                        incident["attachment"].append(
                            {
                                "path": file_result["FileID"],
                                "name": get_attachment_name(attachment.name),
                            }
                        )
                except TypeError as e:
                    if e.message != "must be string or buffer, not None":
                        raise
                    continue
            else:
                # other item attachment
                label_attachment_type = "attachmentItems"
                label_attachment_id_type = "attachmentItemsId"

                # save the attachment
                if attachment.item.mime_content:
                    attached_email = email.message_from_string(
                        attachment.item.mime_content
                    )
                    if attachment.item.headers:
                        attached_email_headers = [
                            (h, " ".join(map(str.strip, v.split("\r\n"))))
                            for (h, v) in list(attached_email.items())
                        ]
                        for header in attachment.item.headers:
                            if (
                                (header.name, header.value)
                                not in attached_email_headers
                                and header.name != "Content-Type"
                            ):
                                attached_email.add_header(header.name, header.value)

                    file_result = fileResult(
                        get_attachment_name(attachment.name) + ".eml",
                        attached_email.as_string(),
                    )

                if file_result:
                    # check for error
                    if file_result["Type"] == entryTypes["error"]:
                        demisto.error(file_result["Contents"])
                        raise Exception(file_result["Contents"])

                    # save attachment to incident
                    incident["attachment"].append(
                        {
                            "path": file_result["FileID"],
                            "name": get_attachment_name(attachment.name) + ".eml",
                        }
                    )

            labels.append(
                {
                    "type": label_attachment_type,
                    "value": get_attachment_name(attachment.name),
                }
            )
            labels.append(
                {"type": label_attachment_id_type, "value": attachment.attachment_id.id}
            )

    # handle headers
    if item.headers:
        headers = []
        for header in item.headers:
            labels.append(
                {
                    "type": "Email/Header/{}".format(header.name),
                    "value": str(header.value),
                }
            )
            headers.append("{}: {}".format(header.name, header.value))
        labels.append({"type": "Email/headers", "value": "\r\n".join(headers)})

    # handle item id
    if item.message_id:
        labels.append({"type": "Email/MessageId", "value": str(item.message_id)})

    if item.item_id:
        labels.append({"type": "Email/ID", "value": item.item_id})
        labels.append({"type": "Email/itemId", "value": item.item_id})

    # handle conversion id
    if item.conversation_id:
        labels.append({"type": "Email/ConversionID", "value": item.conversation_id.id})

    if MARK_AS_READ and is_fetch:
        item.is_read = True
        try:
            item.save()
        except ErrorIrresolvableConflict:
            time.sleep(0.5)
            item.save()

    incident["labels"] = labels
    incident["rawJSON"] = json.dumps(parse_item_as_dict(item, None), ensure_ascii=False)

    return incident


def fetch_emails_as_incidents(client: EWSClient, folder_name):
    last_run = get_last_run()

    try:
        account = client.get_account()
        last_emails = fetch_last_emails(
            account,
            folder_name,
            last_run.get(LAST_RUN_TIME),
            last_run.get(LAST_RUN_IDS),
        )

        ids = deque(last_run.get(LAST_RUN_IDS, []), maxlen=LAST_RUN_IDS_QUEUE_SIZE)
        incidents = []
        incident = {}  # type: Dict[Any, Any]
        for item in last_emails:
            if item.message_id:
                ids.append(item.message_id)
                incident = parse_incident_from_item(item, True)
                incidents.append(incident)

                if len(incidents) >= MAX_FETCH:
                    break

        last_run_time = incident.get("occurred", last_run.get(LAST_RUN_TIME))
        if isinstance(last_run_time, EWSDateTime):
            last_run_time = last_run_time.ewsformat()

        new_last_run = {
            LAST_RUN_TIME: last_run_time,
            LAST_RUN_FOLDER: folder_name,
            LAST_RUN_IDS: list(ids),
            ERROR_COUNTER: 0,
        }

        demisto.setLastRun(new_last_run)
        return incidents

    except RateLimitError:
        if LAST_RUN_TIME in last_run:
            last_run[LAST_RUN_TIME] = last_run[LAST_RUN_TIME].ewsformat()
        if ERROR_COUNTER not in last_run:
            last_run[ERROR_COUNTER] = 0
        last_run[ERROR_COUNTER] += 1
        demisto.setLastRun(last_run)
        if last_run[ERROR_COUNTER] > 2:
            raise
        return []


def get_entry_for_file_attachment(item_id, attachment):
    entry = fileResult(get_attachment_name(attachment.name), attachment.content)
    ec = {
        CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT
        + CONTEXT_UPDATE_FILE_ATTACHMENT: parse_attachment_as_dict(item_id, attachment)
    }
    entry["EntryContext"] = filter_dict_null(ec)
    return entry


def parse_attachment_as_dict(item_id, attachment):
    try:
        attachment_content = (
            attachment.content
            if isinstance(attachment, FileAttachment)
            else attachment.item.mime_content
        )
        return {
            ATTACHMENT_ORIGINAL_ITEM_ID: item_id,
            ATTACHMENT_ID: attachment.attachment_id.id,
            "attachmentName": get_attachment_name(attachment.name),
            "attachmentSHA256": hashlib.sha256(attachment_content).hexdigest()
            if attachment_content
            else None,
            "attachmentContentType": attachment.content_type,
            "attachmentContentId": attachment.content_id,
            "attachmentContentLocation": attachment.content_location,
            "attachmentSize": attachment.size,
            "attachmentLastModifiedTime": attachment.last_modified_time.ewsformat(),
            "attachmentIsInline": attachment.is_inline,
            ATTACHMENT_TYPE: FILE_ATTACHMENT_TYPE
            if isinstance(attachment, FileAttachment)
            else ITEM_ATTACHMENT_TYPE,
        }
    except TypeError as e:
        if e.message != "must be string or buffer, not None":
            raise
        return {
            ATTACHMENT_ORIGINAL_ITEM_ID: item_id,
            ATTACHMENT_ID: attachment.attachment_id.id,
            "attachmentName": get_attachment_name(attachment.name),
            "attachmentSHA256": None,
            "attachmentContentType": attachment.content_type,
            "attachmentContentId": attachment.content_id,
            "attachmentContentLocation": attachment.content_location,
            "attachmentSize": attachment.size,
            "attachmentLastModifiedTime": attachment.last_modified_time.ewsformat(),
            "attachmentIsInline": attachment.is_inline,
            ATTACHMENT_TYPE: FILE_ATTACHMENT_TYPE
            if isinstance(attachment, FileAttachment)
            else ITEM_ATTACHMENT_TYPE,
        }


def get_entry_for_item_attachment(item_id, attachment, target_email):
    item = attachment.item
    dict_result = parse_attachment_as_dict(item_id, attachment)
    dict_result.update(
        parse_item_as_dict(item, target_email, camel_case=True, compact_fields=True)
    )
    title = 'EWS get attachment got item for "%s", "%s"' % (
        target_email,
        get_attachment_name(attachment.name),
    )

    return get_entry_for_object(
        title,
        CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT + CONTEXT_UPDATE_ITEM_ATTACHMENT,
        dict_result,
    )


def get_attachments_for_item(item_id, account, attachment_ids=None):
    item = get_item_from_mailbox(account, item_id)
    attachments = []
    if attachment_ids and not isinstance(attachment_ids, list):
        attachment_ids = attachment_ids.split(",")
    if item:
        if item.attachments:
            for attachment in item.attachments:
                if attachment_ids and attachment.attachment_id.id not in attachment_ids:
                    continue
                attachments.append(attachment)

    else:
        raise Exception("Message item not found: " + item_id)

    if attachment_ids and len(attachments) < len(attachment_ids):
        raise Exception(
            "Some attachment id did not found for message:" + str(attachment_ids)
        )

    return attachments


def delete_attachments_for_message(client: EWSClient, item_id, target_mailbox=None, attachment_ids=None):
    account = client.get_account(target_mailbox)
    attachments = get_attachments_for_item(item_id, account, attachment_ids)
    deleted_file_attachments = []
    deleted_item_attachments = []  # type: ignore
    for attachment in attachments:
        attachment_deleted_action = {
            ATTACHMENT_ID: attachment.attachment_id.id,
            ACTION: "deleted",
        }
        if isinstance(attachment, FileAttachment):
            deleted_file_attachments.append(attachment_deleted_action)
        else:
            deleted_item_attachments.append(attachment_deleted_action)
        attachment.detach()

    entries = []
    if len(deleted_file_attachments) > 0:
        entry = get_entry_for_object(
            "Deleted file attachments",
            "EWS.Items" + CONTEXT_UPDATE_FILE_ATTACHMENT,
            deleted_file_attachments,
        )
        entries.append(entry)
    if len(deleted_item_attachments) > 0:
        entry = get_entry_for_object(
            "Deleted item attachments",
            "EWS.Items" + CONTEXT_UPDATE_ITEM_ATTACHMENT,
            deleted_item_attachments,
        )
        entries.append(entry)

    return entries


def fetch_attachments_for_message(client: EWSClient, item_id, target_mailbox=None, attachment_ids=None):
    account = client.get_account(target_mailbox)
    attachments = get_attachments_for_item(item_id, account, attachment_ids)
    entries = []
    for attachment in attachments:
        if isinstance(attachment, FileAttachment):
            try:
                if attachment.content:
                    entries.append(get_entry_for_file_attachment(item_id, attachment))
            except TypeError as e:
                if str(e) != "must be string or buffer, not None":
                    raise
        else:
            entries.append(
                get_entry_for_item_attachment(
                    item_id, attachment, account.primary_smtp_address
                )
            )
            if attachment.item.mime_content:
                entries.append(
                    fileResult(
                        get_attachment_name(attachment.name) + ".eml",
                        attachment.item.mime_content,
                    )
                )

    return entries


def move_item_between_mailboxes(
    client: EWSClient,
    item_id,
    destination_mailbox,
    destination_folder_path,
    source_mailbox=None,
    is_public=None,
):
    source_account = client.get_account(source_mailbox)
    destination_account = client.get_account(destination_mailbox)
    is_public = is_default_folder(destination_folder_path, is_public)
    destination_folder = get_folder_by_path(
        destination_account, destination_folder_path, is_public
    )
    item = get_item_from_mailbox(source_account, item_id)

    exported_items = source_account.export([item])
    destination_account.upload([(destination_folder, exported_items[0])])
    source_account.bulk_delete([item])

    move_result = {
        MOVED_TO_MAILBOX: destination_mailbox,
        MOVED_TO_FOLDER: destination_folder_path,
    }

    return {
        "Type": entryTypes["note"],
        "Contents": "Item was moved successfully.",
        "ContentsFormat": formats["text"],
        "EntryContext": {"EWS.Items(val.itemId === '%s')" % (item_id,): move_result},
    }


def move_item(client: EWSClient, item_id, target_folder_path, target_mailbox=None, is_public=None):
    account = client.get_account(target_mailbox)
    is_public = is_default_folder(target_folder_path, is_public)
    target_folder = get_folder_by_path(account, target_folder_path, is_public)
    item = get_item_from_mailbox(account, item_id)
    if isinstance(item, ErrorInvalidIdMalformed):
        raise Exception("Item not found")
    item.move(target_folder)
    move_result = {
        NEW_ITEM_ID: item.item_id,
        ITEM_ID: item_id,
        MESSAGE_ID: item.message_id,
        ACTION: "moved",
    }

    return get_entry_for_object("Moved items", CONTEXT_UPDATE_EWS_ITEM, move_result)


def delete_items(client: EWSClient, item_ids, delete_type, target_mailbox=None):
    account = client.get_account(target_mailbox)
    deleted_items = []
    if type(item_ids) != list:
        item_ids = item_ids.split(",")
    items = get_items_from_mailbox(account, item_ids)
    delete_type = delete_type.lower()

    for item in items:
        item_id = item.item_id
        if delete_type == "trash":
            item.move_to_trash()
        elif delete_type == "soft":
            item.soft_delete()
        elif delete_type == "hard":
            item.delete()
        else:
            raise Exception(
                f'invalid delete type: {delete_type}. Use "trash" \\ "soft" \\ "hard"'
            )
        deleted_items.append(
            {
                ITEM_ID: item_id,
                MESSAGE_ID: item.message_id,
                ACTION: f"{delete_type}-deleted",
            }
        )

    return get_entry_for_object(
        f"Deleted items ({delete_type} delete type)",
        CONTEXT_UPDATE_EWS_ITEM,
        deleted_items,
    )


def prepare_args(d):
    d = dict((k.replace("-", "_"), v) for k, v in list(d.items()))
    if "is_public" in d:
        d["is_public"] = d["is_public"] == "True"
    return d


def get_limited_number_of_messages_from_qs(qs, limit):
    count = 0
    results = []
    for item in qs:
        if count == limit:
            break
        if isinstance(item, Message):
            count += 1
            results.append(item)
    return results


def search_items_in_mailbox(
    client: EWSClient,
    query=None,
    message_id=None,
    folder_path="",
    limit=100,
    target_mailbox=None,
    is_public=None,
    selected_fields="all",
):
    if not query and not message_id:
        return_error("Missing required argument. Provide query or message-id")

    if message_id and message_id[0] != "<" and message_id[-1] != ">":
        message_id = "<{}>".format(message_id)

    account = client.get_account(target_mailbox)
    limit = int(limit)
    if folder_path.lower() == "inbox":
        folders = [account.inbox]
    elif folder_path:
        is_public = is_default_folder(folder_path, is_public)
        folders = [get_folder_by_path(account, folder_path, is_public)]
    else:
        folders = account.inbox.parent.walk()  # pylint: disable=E1101

    items = []  # type: ignore
    selected_all_fields = selected_fields == "all"

    if selected_all_fields:
        restricted_fields = list([x.name for x in Message.FIELDS])  # type: ignore
    else:
        restricted_fields = set(argToList(selected_fields))  # type: ignore
        restricted_fields.update(["id", "message_id"])  # type: ignore

    for folder in folders:
        if Message not in folder.supported_item_models:
            continue
        if query:
            items_qs = folder.filter(query).only(*restricted_fields)
        else:
            items_qs = folder.filter(message_id=message_id).only(*restricted_fields)
        items += get_limited_number_of_messages_from_qs(items_qs, limit)
        if len(items) >= limit:
            break

    items = items[:limit]
    searched_items_result = [
        parse_item_as_dict(
            item,
            account.primary_smtp_address,
            camel_case=True,
            compact_fields=selected_all_fields,
        )
        for item in items
    ]

    if not selected_all_fields:
        searched_items_result = [
            {k: v for (k, v) in i.items() if k in keys_to_camel_case(restricted_fields)}
            for i in searched_items_result
        ]

        for item in searched_items_result:
            item["itemId"] = item.pop("id", "")

    return get_entry_for_object(
        "Searched items",
        CONTEXT_UPDATE_EWS_ITEM,
        searched_items_result,
        headers=ITEMS_RESULTS_HEADERS if selected_all_fields else None,
    )


def get_out_of_office_state(client: EWSClient, target_mailbox=None):
    account = client.get_account(target_mailbox)
    oof = account.oof_settings
    oof_dict = {
        "state": oof.state,  # pylint: disable=E1101
        "externalAudience": getattr(oof, "external_audience", None),
        "start": oof.start.ewsformat() if oof.start else None,  # pylint: disable=E1101
        "end": oof.end.ewsformat() if oof.end else None,  # pylint: disable=E1101
        "internalReply": getattr(oof, "internal_replay", None),
        "externalReply": getattr(oof, "external_replay", None),
        MAILBOX: account.primary_smtp_address,
    }
    return get_entry_for_object(
        "Out of office state for %s" % account.primary_smtp_address,
        "Account.Email(val.Address == obj.{0}).OutOfOffice".format(MAILBOX),
        oof_dict,
    )


def recover_soft_delete_item(
    client: EWSClient, message_ids, target_folder_path="Inbox", target_mailbox=None, is_public=None
):
    account = client.get_account(target_mailbox)
    is_public = is_default_folder(target_folder_path, is_public)
    target_folder = get_folder_by_path(account, target_folder_path, is_public)
    recovered_messages = []
    if type(message_ids) != list:
        message_ids = message_ids.split(",")
    items_to_recover = account.recoverable_items_deletions.filter(  # pylint: disable=E1101
        message_id__in=message_ids
    ).all()  # pylint: disable=E1101
    if len(items_to_recover) != len(message_ids):
        raise Exception("Some message ids are missing in recoverable items directory")
    for item in items_to_recover:
        item.move(target_folder)
        recovered_messages.append(
            {ITEM_ID: item.item_id, MESSAGE_ID: item.message_id, ACTION: "recovered"}
        )
    return get_entry_for_object(
        "Recovered messages", CONTEXT_UPDATE_EWS_ITEM, recovered_messages
    )


def get_contacts(client: EWSClient, limit, target_mailbox=None):
    def parse_physical_address(address):
        result = {}
        for attr in ["city", "country", "label", "state", "street", "zipcode"]:
            result[attr] = getattr(address, attr, None)
        return result

    def parse_phone_number(phone_number):
        result = {}
        for attr in ["label", "phone_number"]:
            result[attr] = getattr(phone_number, attr, None)
        return result

    def parse_contact(contact):
        contact_dict = dict(
            (k, v if not isinstance(v, EWSDateTime) else v.ewsformat())
            for k, v in list(contact.__dict__.items())
            if isinstance(v, str) or isinstance(v, EWSDateTime)
        )
        if isinstance(contact, Contact) and contact.physical_addresses:
            contact_dict["physical_addresses"] = list(
                map(parse_physical_address, contact.physical_addresses)
            )
        if isinstance(contact, Contact) and contact.phone_numbers:
            contact_dict["phone_numbers"] = list(
                map(parse_phone_number, contact.phone_numbers)
            )
        if (
            isinstance(contact, Contact)
            and contact.email_addresses
            and len(contact.email_addresses) > 0
        ):
            contact_dict["emailAddresses"] = [x.email for x in contact.email_addresses]
        contact_dict = keys_to_camel_case(contact_dict)
        contact_dict = dict((k, v) for k, v in list(contact_dict.items()) if v)
        del contact_dict["mimeContent"]
        contact_dict["originMailbox"] = target_mailbox
        return contact_dict

    account = client.get_account(target_mailbox)
    contacts = []

    for contact in account.contacts.all()[: int(limit)]:  # pylint: disable=E1101
        contacts.append(parse_contact(contact))
    return get_entry_for_object(
        "Email contacts for %s" % target_mailbox,
        "Account.Email(val.Address == obj.originMailbox).EwsContacts",
        contacts,
    )


def create_folder(client: EWSClient, new_folder_name, folder_path, target_mailbox=None):
    account = client.get_account(target_mailbox)
    full_path = "%s\\%s" % (folder_path, new_folder_name)
    try:
        if get_folder_by_path(account, full_path):
            return "Folder %s already exists" % full_path
    except Exception:
        pass
    parent_folder = get_folder_by_path(account, folder_path)
    f = Folder(parent=parent_folder, name=new_folder_name)
    f.save()
    get_folder_by_path(account, full_path)
    return "Folder %s created successfully" % full_path


def find_folders(client: EWSClient, target_mailbox=None):
    account = client.get_account(target_mailbox)
    root = account.root
    # if exchangelib.__version__ == "1.12.0":  # Docker BC
    #     if is_public:
    #         root = account.public_folders_root todo: remove if need be
    folders = []
    for f in root.walk():  # pylint: disable=E1101
        folder = folder_to_context_entry(f)
        folders.append(folder)
    folders_tree = root.tree()  # pylint: disable=E1101

    return {
        "Type": entryTypes["note"],
        "Contents": folders,
        "ContentsFormat": formats["json"],
        "ReadableContentsFormat": formats["text"],
        "HumanReadable": folders_tree,
        "EntryContext": {"EWS.Folders(val.id == obj.id)": folders},
    }


def mark_item_as_junk(client: EWSClient, item_id, move_items, target_mailbox=None):
    account = client.get_account(target_mailbox)
    move_items = move_items.lower() == "yes"
    ews_result = MarkAsJunk(account=account).call(item_id=item_id, move_item=move_items)
    mark_as_junk_result = {
        ITEM_ID: item_id,
    }
    if ews_result == "Success":
        mark_as_junk_result[ACTION] = "marked-as-junk"
    else:
        raise Exception("Failed mark-item-as-junk with error: " + ews_result)

    return get_entry_for_object(
        "Mark item as junk", CONTEXT_UPDATE_EWS_ITEM, mark_as_junk_result
    )


def get_items_from_folder(
    client: EWSClient, folder_path, limit=100, target_mailbox=None, is_public=None, get_internal_item="no"
):
    account = client.get_account(target_mailbox)
    limit = int(limit)
    get_internal_item = get_internal_item == "yes"
    is_public = is_default_folder(folder_path, is_public)
    folder = get_folder_by_path(account, folder_path, is_public)
    qs = folder.filter().order_by("-datetime_created")[:limit]
    items = get_limited_number_of_messages_from_qs(qs, limit)
    items_result = []

    for item in items:
        item_attachment = parse_item_as_dict(
            item, account.primary_smtp_address, camel_case=True, compact_fields=True
        )
        for attachment in item.attachments:
            if (
                get_internal_item
                and isinstance(attachment, ItemAttachment)
                and isinstance(attachment.item, Message)
            ):
                # if found item attachment - switch item to the attchment
                item_attachment = parse_item_as_dict(
                    attachment.item,
                    account.primary_smtp_address,
                    camel_case=True,
                    compact_fields=True,
                )
                break
        items_result.append(item_attachment)

    hm_headers = [
        "sender",
        "subject",
        "hasAttachments",
        "datetimeReceived",
        "receivedBy",
        "author",
        "toRecipients",
    ]
    # if exchangelib.__version__ == "1.12.0":  # Docker BC
    #     hm_headers.append("itemId") todo: remove if need be
    return get_entry_for_object(
        "Items in folder " + folder_path,
        CONTEXT_UPDATE_EWS_ITEM,
        items_result,
        headers=hm_headers,
    )


def get_items(client: EWSClient, item_ids, target_mailbox=None):
    account = client.get_account(target_mailbox)
    if type(item_ids) != list:
        item_ids = item_ids.split(",")

    items = get_items_from_mailbox(account, item_ids)
    items = [x for x in items if isinstance(x, Message)]
    items_as_incidents = [parse_incident_from_item(x, False) for x in items]
    items_to_context = [
        parse_item_as_dict(x, account.primary_smtp_address, True, True) for x in items
    ]

    return {
        "Type": entryTypes["note"],
        "Contents": items_as_incidents,
        "ContentsFormat": formats["json"],
        "ReadableContentsFormat": formats["markdown"],
        "HumanReadable": tableToMarkdown(
            "Get items", items_to_context, ITEMS_RESULTS_HEADERS
        ),
        "EntryContext": {
            CONTEXT_UPDATE_EWS_ITEM: items_to_context,
            "Email": [email_ec(item) for item in items],
        },
    }


def get_folder(client: EWSClient, folder_path, target_mailbox=None, is_public=None):
    account = client.get_account(target_mailbox)
    is_public = is_default_folder(folder_path, is_public)
    folder = folder_to_context_entry(
        get_folder_by_path(account, folder_path, is_public)
    )
    return get_entry_for_object(
        "Folder %s" % (folder_path,), CONTEXT_UPDATE_FOLDER, folder
    )


def folder_to_context_entry(f):
    f_entry = {
        "name": f.name,
        "totalCount": f.total_count,
        "id": f.id,
        "childrenFolderCount": f.child_folder_count,
        "changeKey": f.changekey,
    }

    if "unread_count" in [x.name for x in Folder.FIELDS]:
        f_entry["unreadCount"] = f.unread_count
    return f_entry


def mark_item_as_read(client: EWSClient, item_ids, operation="read", target_mailbox=None):
    marked_items = []
    account = client.get_account(target_mailbox)
    item_ids = argToList(item_ids)
    items = get_items_from_mailbox(account, item_ids)
    items = [x for x in items if isinstance(x, Message)]

    for item in items:
        item.is_read = operation == "read"
        item.save()

        marked_items.append(
            {
                ITEM_ID: item.item_id,
                MESSAGE_ID: item.message_id,
                ACTION: "marked-as-{}".format(operation),
            }
        )

    return get_entry_for_object(
        "Marked items ({} marked operation)".format(operation),
        CONTEXT_UPDATE_EWS_ITEM,
        marked_items,
    )


def get_item_as_eml(client: EWSClient, item_id, target_mailbox=None):
    account = client.get_account(target_mailbox)
    item = get_item_from_mailbox(account, item_id)

    if item.mime_content:
        email_content = email.message_from_string(item.mime_content)
        if item.headers:
            attached_email_headers = [
                (h, " ".join(map(str.strip, v.split("\r\n"))))
                for (h, v) in list(email_content.items())
            ]
            for header in item.headers:
                if (
                    header.name,
                    header.value,
                ) not in attached_email_headers and header.name != "Content-Type":
                    email_content.add_header(header.name, header.value)

        eml_name = item.subject if item.subject else "demisto_untitled_eml"
        file_result = fileResult(eml_name + ".eml", email_content.as_string())
        file_result = (
            file_result if file_result else "Failed uploading eml file to war room"
        )

        return file_result


def test_module(client: EWSClient):
    try:
        global IS_TEST_MODULE
        IS_TEST_MODULE = True
        account = client.get_account()
        if not account.root.effective_rights.read:  # pylint: disable=E1101
            raise Exception(
                "Success to authenticate, but user has no permissions to read from the mailbox. "
                "Need to delegate the user permissions to the mailbox - "
                "please read integration documentation and follow the instructions"
            )
        get_folder_by_path(account, FOLDER_NAME, IS_PUBLIC_FOLDER).test_access()
    except ErrorFolderNotFound as e:
        if "Top of Information Store" in str(e):
            raise Exception(
                "Success to authenticate, but user probably has no permissions to read from the specific folder."
                "Check user permissions. You can try !ews-find-folders command to "
                "get all the folders structure that the user has permissions to"
            )

    demisto.results("ok")


def sub_main():
    global USERNAME, PASSWORD
    global config, credentials
    params = demisto.params()
    client = EWSClient(**params)
    USERNAME = params.get("credentials", {})["identifier"]
    PASSWORD = params.get("credentials", {})["password"]
    insecure = params.get("insecure", True)
    config, credentials = prepare(insecure)
    protocol = BaseProtocol(config)
    args = prepare_args(demisto.args())
    start_logging()
    try:
        if demisto.command() == "test-module":
            test_module(client)
        elif demisto.command() == "fetch-incidents":
            incidents = fetch_emails_as_incidents(client, FOLDER_NAME)
            demisto.incidents(incidents)
        elif demisto.command() == "ews-get-attachment":
            demisto.results(fetch_attachments_for_message(client, **args))
        elif demisto.command() == "ews-delete-attachment":
            demisto.results(delete_attachments_for_message(client, **args))
        elif demisto.command() == "ews-get-searchable-mailboxes":
            demisto.results(get_searchable_mailboxes(protocol))
        elif demisto.command() == "ews-search-mailboxes":
            demisto.results(search_mailboxes(protocol, **args))
        elif demisto.command() == "ews-move-item-between-mailboxes":
            demisto.results(move_item_between_mailboxes(client, **args))
        elif demisto.command() == "ews-move-item":
            demisto.results(move_item(client, **args))
        elif demisto.command() == "ews-delete-items":
            demisto.results(delete_items(client, **args))
        elif demisto.command() == "ews-search-mailbox":
            demisto.results(search_items_in_mailbox(**args))
        elif demisto.command() == "ews-get-contacts":
            demisto.results(get_contacts(client, **args))
        elif demisto.command() == "ews-get-out-of-office":
            demisto.results(get_out_of_office_state(client, **args))
        elif demisto.command() == "ews-recover-messages":
            demisto.results(recover_soft_delete_item(client, **args))
        elif demisto.command() == "ews-create-folder":
            demisto.results(create_folder(client, **args))
        elif demisto.command() == "ews-mark-item-as-junk":
            demisto.results(mark_item_as_junk(client, **args))
        elif demisto.command() == "ews-find-folders":
            demisto.results(find_folders(**args))
        elif demisto.command() == "ews-get-items-from-folder":
            demisto.results(get_items_from_folder(client, **args))
        elif demisto.command() == "ews-get-items":
            demisto.results(get_items(client, **args))
        elif demisto.command() == "ews-get-folder":
            demisto.results(get_folder(**args))
        elif demisto.command() == "ews-expand-group":
            demisto.results(get_expanded_group(protocol, **args))
        elif demisto.command() == "ews-mark-items-as-read":
            demisto.results(mark_item_as_read(client, **args))
        elif demisto.command() == "ews-get-items-as-eml":
            demisto.results(get_item_as_eml(client, **args))

    except Exception as e:
        time.sleep(2)
        start_logging()
        debug_log = log_stream.getvalue()  # type: ignore
        error_message_simple = ""

        # Office365 regular maintenance case
        if (
            isinstance(e, ErrorMailboxStoreUnavailable)
            or isinstance(e, ErrorMailboxMoveInProgress)
        ):
            log_message = (
                "Office365 is undergoing load balancing operations. "
                "As a result, the service is temporarily unavailable."
            )
            if demisto.command() == "fetch-incidents":
                demisto.info(log_message)
                demisto.incidents([])
                sys.exit(0)
            if IS_TEST_MODULE:
                demisto.results(
                    log_message + " Please retry the instance configuration test."
                )
                sys.exit(0)
            error_message_simple = log_message + " Please retry your request."

        if isinstance(e, ConnectionError):
            error_message_simple = (
                "Could not connect to the server.\n"
                "Verify that the Hostname or IP address is correct.\n\n"
                f"Additional information: {str(e)}"
            )
        if isinstance(e, ErrorInvalidPropertyRequest):
            error_message_simple = "Verify that the Exchange version is correct."
        else:
            if IS_TEST_MODULE and isinstance(e, MalformedResponseError):
                error_message_simple = (
                    "Got invalid response from the server.\n"
                    "Verify that the Hostname or IP address is is correct."
                )

        # Legacy error handling
        if "Status code: 401" in debug_log:
            error_message_simple = (
                "Got unauthorized from the server. "
                "Check credentials are correct and authentication method are supported. "
            )

        if "Status code: 503" in debug_log:
            error_message_simple = (
                "Got timeout from the server. "
                "Probably the server is not reachable with the current settings. "
                "Check proxy parameter. If you are using server URL - change to server IP address. "
            )

        if not error_message_simple:
            error_message = error_message_simple = str(e)
        else:
            error_message = error_message_simple + "\n" + str(e)

        stacktrace = traceback.format_exc()
        if stacktrace:
            error_message += "\nFull stacktrace:\n" + stacktrace

        if debug_log:
            error_message += "\nFull debug log:\n" + debug_log

        if demisto.command() == "fetch-incidents":
            raise
        if demisto.command() == "ews-search-mailbox" and isinstance(e, ValueError):
            return_error(
                message="Selected invalid field, please specify valid field name.",
                error=e,
            )
        if IS_TEST_MODULE:
            demisto.results(error_message_simple)
        else:
            demisto.results(
                {
                    "Type": entryTypes["error"],
                    "ContentsFormat": formats["text"],
                    "Contents": error_message_simple,
                }
            )
        demisto.error("%s: %s" % (e.__class__.__name__, error_message))
    finally:
        exchangelib_cleanup()
        if log_stream:
            try:
                logging.getLogger().removeHandler(log_handler)  # type: ignore
                log_stream.close()
            except Exception as ex:
                demisto.error(
                    "EWS: unexpected exception when trying to remove log handler: {}".format(
                        ex
                    )
                )


def process_main():
    """setup stdin to fd=0 so we can read from the server"""
    sys.stdin = os.fdopen(0, "r")
    sub_main()


def main():
    # When running big queries, like 'ews-search-mailbox' the memory might not freed by the garbage
    # collector. `separate_process` flag will run the integration on a separate process that will prevent
    # memory leakage.
    separate_process = demisto.params().get("separate_process", False)
    demisto.debug("Running as separate_process: {}".format(separate_process))
    if separate_process:
        try:
            p = Process(target=process_main)
            p.start()
            p.join()
        except Exception as ex:
            demisto.error("Failed starting Process: {}".format(ex))
    else:
        sub_main()


if __name__ in ("__main__", "__builtin__", "builtins"):
    main()
