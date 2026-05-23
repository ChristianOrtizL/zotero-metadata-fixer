#!/usr/bin/env python3
"""
2026_05_21_zotero_metadata_fixer_v3.py

v3: promote standalone PDF attachments (no parentItem) into proper
    journalArticle items by extracting DOIs from the PDF text using
    pypdf, then creating a new parent via the Zotero API and linking
    the attachment underneath it.

    Also inherits all v2 fixes:
    - Fixes top-level items with Bioz/PMC/PMID/bioRxiv junk titles
    - Skips ALL child items (parentItem set) to avoid 400 from API

Identifier resolution order for standalone PDFs:
  1. DOI regex in PDF text (pages 1-2)    -> CrossRef
  2. PMID regex in PDF text               -> NCBI -> CrossRef
  3. bioRxiv pattern in filename          -> 10.1101/<id>

Usage:
  export ZOTERO_LIBRARY_ID=1234567
  export ZOTERO_API_KEY=xxxxx
  export NCBI_EMAIL=you@ucsc.edu

  pip install pypdf

  # Dry run (default): see what would change, write nothing
  python 2026_05_21_zotero_metadata_fixer_v3.py

  # Test on one standalone attachment key
  python 2026_05_21_zotero_metadata_fixer_v3.py --key ABCD1234

  # Limit to N promotions
  python 2026_05_21_zotero_metadata_fixer_v3.py --limit 3

  # Actually write changes
  python 2026_05_21_zotero_metadata_fixer_v3.py --apply
"""

import os
import re
import sys
import time
import logging
import argparse
import tempfile

import requests
from pyzotero import zotero

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# --- CONFIG (env vars) ---
LIBRARY_ID = os.environ.get('ZOTERO_LIBRARY_ID', '')
API_KEY = os.environ.get('ZOTERO_API_KEY', '')
EMAIL = os.environ.get('NCBI_EMAIL', '')
LIBRARY_TYPE = 'user'  # change to 'group' for group libraries

# --- PATTERNS ---
# Bioz/PMC snapshots: title like "pmc10948791 | Bioz | Ratings..."
PMC_RE = re.compile(r'\bpmc(\d{6,9})\b', re.IGNORECASE)
# PubMed snapshots: title like "pm38460515 | ..."
PMID_RE = re.compile(r'\bpm(\d{6,9})\b', re.IGNORECASE)
# bioRxiv filename: 2020.08.02.232785v1.full.pdf -> 10.1101/2020.08.02.232785
BIORXIV_RE = re.compile(r'(\d{4}\.\d{2}\.\d{2}\.\d{6})')
# DOI anywhere in text: 10.XXXX/anything-non-whitespace
DOI_RE = re.compile(r'\b(10\.\d{4,}/[^\s"\'<>]+)', re.IGNORECASE)
# Bare PMID in text: "PMID: 12345678" or "PMID12345678"
PMID_TEXT_RE = re.compile(r'PMID[:\s]*(\d{6,9})\b', re.IGNORECASE)


# --- PDF TEXT EXTRACTION ---
def extract_pdf_text(pdf_path, max_pages=2):
    """Extract text from the first max_pages pages of a PDF. Returns '' on failure."""
    if not HAS_PYPDF:
        return ''
    try:
        reader = PdfReader(pdf_path)
        text = ''
        for page in reader.pages[:max_pages]:
            text += page.extract_text() or ''
        return text
    except Exception as e:
        logging.debug('  [pdf] extract failed: %s', e)
        return ''


def doi_from_pdf_text(text, filename=''):
    """Try to extract a DOI or PMID from PDF text + filename."""
    # 1. DOI in text
    m = DOI_RE.search(text)
    if m:
        doi = m.group(1).rstrip('.,;)')
        logging.info('  DOI from PDF text -> %s', doi)
        return doi

    # 2. PMID in text -> NCBI -> DOI
    m = PMID_TEXT_RE.search(text)
    if m:
        logging.info('  PMID %s from PDF text -> NCBI lookup', m.group(1))
        return ncbi_id_to_doi(m.group(1))

    # 3. bioRxiv in filename
    m = BIORXIV_RE.search(filename)
    if m:
        doi = f'10.1101/{m.group(1)}'
        logging.info('  bioRxiv filename -> %s', doi)
        return doi

    return None


# --- IDENTIFIER LOOKUPS ---
def ncbi_id_to_doi(any_id):
    """Resolve PMC or PMID to DOI via NCBI ID converter."""
    r = requests.get(
        'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/',
        params={
            'ids': any_id,
            'format': 'json',
            'tool': 'zotero-metadata-fixer',
            'email': EMAIL,
        },
        timeout=15,
    )
    r.raise_for_status()
    records = r.json().get('records', [])
    if not records:
        return None
    rec = records[0]
    if 'status' in rec and rec['status'] == 'error':
        return None
    return rec.get('doi')


def crossref_lookup(doi):
    """Fetch full metadata for a DOI from CrossRef."""
    r = requests.get(
        f'https://api.crossref.org/works/{doi}',
        headers={'User-Agent': f'zotero-metadata-fixer (mailto:{EMAIL})'},
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()['message']


# --- MAPPING ---
def crossref_to_zotero_fields(cr):
    """Map CrossRef 'message' object to Zotero journalArticle fields."""
    creators = [
        {
            'creatorType': 'author',
            'firstName': a.get('given', ''),
            'lastName': a.get('family', a.get('name', '')),
        }
        for a in cr.get('author', [])
    ]

    # CrossRef dates come as nested lists, e.g. [[2024, 4, 10]]
    date_parts = (
        cr.get('published-print', {}).get('date-parts')
        or cr.get('published-online', {}).get('date-parts')
        or cr.get('issued', {}).get('date-parts')
        or [[]]
    )
    date = '-'.join(str(p) for p in date_parts[0]) if date_parts[0] else ''

    abstract = cr.get('abstract', '')
    # CrossRef wraps abstracts in JATS tags
    abstract = re.sub(r'</?jats:[^>]+>', '', abstract).strip()

    return {
        'itemType': 'journalArticle',
        'title': (cr.get('title') or [''])[0],
        'creators': creators,
        'publicationTitle': (cr.get('container-title') or [''])[0],
        'volume': cr.get('volume', ''),
        'issue': cr.get('issue', ''),
        'pages': cr.get('page', ''),
        'date': date,
        'DOI': cr.get('DOI', ''),
        'abstractNote': abstract,
        'url': cr.get('URL', ''),
    }


# --- DETECTION ---
def detect_doi(item):
    """Inspect a Zotero item; return a DOI string or None."""
    data = item['data']
    title = data.get('title', '') or ''
    filename = data.get('filename', '') or ''

    # Already has a DOI -> nothing to do
    if data.get('DOI'):
        return None

    # 1. PMC in title (Bioz snapshots)
    m = PMC_RE.search(title)
    if m:
        logging.info('  PMC%s -> NCBI lookup', m.group(1))
        return ncbi_id_to_doi(f'PMC{m.group(1)}')

    # 2. PMID in title
    m = PMID_RE.search(title)
    if m:
        logging.info('  PMID %s -> NCBI lookup', m.group(1))
        return ncbi_id_to_doi(m.group(1))

    # 3. bioRxiv pattern in filename or title
    m = BIORXIV_RE.search(filename) or BIORXIV_RE.search(title)
    if m:
        doi = f'10.1101/{m.group(1)}'
        logging.info('  bioRxiv pattern -> %s', doi)
        return doi

    return None


# --- FIX ---
def fix_item(zot, item, dry_run=True):
    """Resolve DOI for one item, fetch CrossRef, update Zotero record."""
    title = (item['data'].get('title') or '')[:80]
    itype = item['data'].get('itemType', '?')

    # Standalone attachments need restructuring (create parent),
    # which v1 does not do. Report and skip.
    parent = item['data'].get('parentItem')

    # Attachments: standalone ones get promotion attempt (v3);
    # child attachments are skipped (can't change itemType of a child).
    if itype == 'attachment':
        if parent:
            logging.info('  [skip] attachment (child of %s); cannot convert', parent)
            return False
        else:
            # Standalone: try to promote to journalArticle parent
            return promote_standalone(zot, item, dry_run=dry_run)

    # Non-attachment items that are CHILDREN of another item also can't be
    # journalArticle. Zotero only allows note/attachment/annotation as
    # children. This catches HTML snapshots saved as child webpage items.
    if parent:
        logging.info('  [skip] child item (parent=%s); cannot promote to journalArticle', parent)
        return False

    try:
        doi = detect_doi(item)
    except requests.HTTPError as e:
        logging.warning('  [fail] ID lookup HTTP error: %s', e)
        return False
    except requests.RequestException as e:
        logging.warning('  [fail] ID lookup network error: %s', e)
        return False

    if not doi:
        return False

    try:
        cr = crossref_lookup(doi)
    except requests.HTTPError as e:
        logging.warning('  [fail] CrossRef HTTP error for %s: %s', doi, e)
        return False
    except requests.RequestException as e:
        logging.warning('  [fail] CrossRef network error for %s: %s', doi, e)
        return False

    if not cr:
        logging.warning('  [fail] no CrossRef record for %s', doi)
        return False

    new_fields = crossref_to_zotero_fields(cr)
    new_title = new_fields['title'][:80]
    logging.info('  rename: "%s" -> "%s"', title, new_title)

    if dry_run:
        return True

    # Merge new fields into existing item dict, then ship the whole thing
    # back. pyzotero requires the full item structure with its current
    # 'version' for optimistic concurrency control.
    item['data'].update(new_fields)
    zot.update_item(item)
    return True


# --- PROMOTE STANDALONE ATTACHMENT ---
def promote_standalone(zot, item, dry_run=True):
    """
    For a standalone PDF attachment (no parentItem):
      1. Download the PDF from Zotero storage
      2. Extract text from pages 1-2
      3. Find DOI/PMID
      4. CrossRef lookup
      5. Create a new journalArticle parent
      6. Patch the attachment's parentItem to point at the new parent
    """
    key = item['key']
    filename = item['data'].get('filename', '') or ''
    title = (item['data'].get('title') or filename)[:80]

    if item['data'].get('contentType', '') != 'application/pdf':
        logging.info('  [skip] not a PDF attachment (%s)',
                     item['data'].get('contentType', 'unknown'))
        return False

    if not HAS_PYPDF:
        logging.warning('  [skip] pypdf not installed; run: python -m pip install pypdf')
        return False

    # Download the attachment bytes from Zotero API
    try:
        pdf_bytes = zot.file(key)
    except Exception as e:
        logging.warning('  [fail] could not download attachment %s: %s', key, e)
        return False

    # Write to a temp file so pypdf can read it
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        text = extract_pdf_text(tmp_path)
    finally:
        os.unlink(tmp_path)

    doi = doi_from_pdf_text(text, filename)
    if not doi:
        logging.info('  [fail] no DOI/PMID found in PDF text or filename')
        return False

    try:
        cr = crossref_lookup(doi)
    except requests.RequestException as e:
        logging.warning('  [fail] CrossRef error for %s: %s', doi, e)
        return False

    if not cr:
        logging.warning('  [fail] no CrossRef record for %s', doi)
        return False

    new_fields = crossref_to_zotero_fields(cr)
    new_title = new_fields['title'][:80]
    logging.info('  promote: "%s" -> parent "%s"', title, new_title)

    if dry_run:
        return True

    # Create the parent journalArticle
    parent_resp = zot.create_items([new_fields])
    # pyzotero returns {'successful': {'0': {item}}, 'failed': {...}}
    successful = parent_resp.get('successful', {})
    if not successful:
        logging.warning('  [fail] parent create failed: %s', parent_resp)
        return False

    parent_key = successful['0']['key']
    logging.info('  created parent %s', parent_key)

    # Link the attachment to its new parent.
    # Strip attachment-only fields pyzotero's validator rejects
    # (lastRead, md5, mtime, charset are not in its known-fields list).
    item['data']['parentItem'] = parent_key
    for field in ('lastRead', 'md5', 'mtime', 'charset', 'collections'):
        item['data'].pop(field, None)
    zot.update_item(item)
    logging.info('  linked attachment %s -> parent %s', key, parent_key)
    return True


# --- MAIN ---
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='Write changes. Without this flag, dry-run only.')
    parser.add_argument('--limit', type=int, default=None,
                        help='Stop after N successful fixes.')
    parser.add_argument('--key', type=str, default=None,
                        help='Process only this single Zotero item key.')
    parser.add_argument('--sleep', type=float, default=0.3,
                        help='Seconds between items (be nice to NCBI/CrossRef).')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if not LIBRARY_ID or not API_KEY:
        sys.exit('ERROR: set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY env vars.')
    if not EMAIL:
        sys.exit('ERROR: set NCBI_EMAIL env var (NCBI etiquette + higher rate).')

    zot = zotero.Zotero(LIBRARY_ID, LIBRARY_TYPE, API_KEY)

    if args.key:
        items = [zot.item(args.key)]
    else:
        items = zot.everything(zot.items())

    fixed, skipped = 0, 0
    for i, item in enumerate(items):
        if args.limit is not None and fixed >= args.limit:
            break
        title = (item['data'].get('title') or '')[:60]
        logging.info('[%d] %s | %s', i, item['key'], title)

        if fix_item(zot, item, dry_run=not args.apply):
            fixed += 1
        else:
            skipped += 1

        time.sleep(args.sleep)

    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    logging.info('\n[%s] fixed: %d, skipped: %d', mode, fixed, skipped)


if __name__ == '__main__':
    main()
