#!/usr/bin/env python3
"""
2026_05_21_zotero_metadata_fixer_v1.py

Scan a Zotero library, find items with bad/missing metadata,
resolve DOIs from identifiers embedded in titles or filenames,
fetch clean metadata from CrossRef, and patch the items via the
Zotero API.

Detects three patterns:
  1. Title contains 'pmcXXXXXXXX'  -> NCBI ID converter -> DOI
  2. Title contains 'pmXXXXXXXX'   -> NCBI ID converter (PMID) -> DOI
  3. Filename matches bioRxiv      -> 10.1101/<YYYY.MM.DD.NNNNNN>
     (e.g. 2020.08.02.232785v1.full.pdf)

For each resolved DOI it calls CrossRef, maps the record to Zotero
fields, and updates the item. Skips items that already have a DOI.

Standalone PDF attachments (no parent item) are reported but not
restructured in this version. v2 can promote them into proper
journalArticle parents.

Usage:
  export ZOTERO_LIBRARY_ID=1234567
  export ZOTERO_API_KEY=xxxxx
  export NCBI_EMAIL=you@ucsc.edu

  # Dry run (default): see what would change, write nothing
  python 2026_05_21_zotero_metadata_fixer_v1.py

  # Test on one item first
  python 2026_05_21_zotero_metadata_fixer_v1.py --key ABCD1234

  # Limit to N changes
  python 2026_05_21_zotero_metadata_fixer_v1.py --limit 3

  # Actually write changes
  python 2026_05_21_zotero_metadata_fixer_v1.py --apply
"""

import os
import re
import sys
import time
import logging
import argparse

import requests
from pyzotero import zotero

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
    if itype == 'attachment' and not item['data'].get('parentItem'):
        logging.info('  [skip] standalone attachment; needs parent (v2)')
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
