"""A REST API műveleteinek listája (a dokumentációs oldal, az OpenAPI és az MCP-leírás közös forrása)."""
from __future__ import annotations

from app.catalog import tools as T
from app.site import i18n

IMPORTS = {
    "import/upload": "Upload a file: the result gives a one-time upload URL (multipart, or chunked for large files).",
    "import/url": "Download a file from a public http(s) URL.",
    "import/base64": "A small file given inline as base64 (up to 10 MB).",
    "import/file": "A file already in your workspace (file_id).",
    "import/s3": "From Amazon S3 or an S3-compatible store (bucket, key, credentials, endpoint).",
    "import/azure": "From Azure Blob Storage (container, blob, SAS token or connection string).",
    "import/gcs": "From Google Cloud Storage (bucket, object, service-account JSON).",
    "import/sftp": "From an SFTP server (host, user, password or private key, path).",
    "import/google-drive": "From Google Drive (file id + OAuth access token).",
    "import/dropbox": "From Dropbox (path + access token).",
    "import/onedrive": "From OneDrive / SharePoint (item id or path + access token).",
}
EXPORTS = {
    "export/url": "A temporary signed download link for each result file.",
    "export/s3": "Upload the results to Amazon S3 or an S3-compatible store.",
    "export/azure": "Upload the results to Azure Blob Storage.",
    "export/gcs": "Upload the results to Google Cloud Storage.",
    "export/sftp": "Upload the results to an SFTP server.",
    "export/google-drive": "Upload the results to Google Drive.",
    "export/dropbox": "Upload the results to Dropbox.",
    "export/onedrive": "Upload the results to OneDrive.",
}
CORE = {
    "convert": "Convert the input to output_format (any of the supported directions), with options.",
    "archive": "Pack all inputs into one archive (zip, 7z, tar.gz…).",
    "merge": "Merge inputs into one file (PDF, video or audio).",
    "thumbnail": "A preview image of the input.",
    "metadata": "Read the metadata of the input (JSON result).",
    "capture-website": "A web page as PDF or image (url, no input).",
}


def catalog_ops() -> list[tuple[str, str]]:
    ops = [*IMPORTS.items(), *CORE.items()]
    for t in T.live(static=True):
        ops.append((t.operation, i18n.raw(i18n.DEFAULT, f"tool.{t.key}.desc", t.key)))
    ops += list(EXPORTS.items())
    return ops
