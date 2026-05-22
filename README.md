# zotero-metadata-fixer

Scan a Zotero library, find items with junk titles, resolve real DOIs
from identifiers embedded in titles or filenames, fetch clean metadata
from CrossRef, and patch the items via the Zotero API.

Built to clean up items saved from Bioz (which titles things like
`pmc10948791 | Bioz | Ratings For Life-Science Research`) and from
bioRxiv PDFs named `2020.08.02.232785v1.full.pdf`.

## Detects three patterns

- `pmcXXXXXXXX` in title -> NCBI ID converter -> DOI
- `pmXXXXXXXX` in title -> NCBI PMID lookup -> DOI
- bioRxiv filename pattern `YYYY.MM.DD.NNNNNN` -> `10.1101/<id>`

## Setup

```bash
pip install -r requirements.txt

export ZOTERO_LIBRARY_ID=your_user_id
export ZOTERO_API_KEY=your_api_key
export NCBI_EMAIL=you@email.com
```

Get your user ID and API key at https://www.zotero.org/settings/keys

## Usage

```bash
# Dry run (default, no writes)
python 2026_05_21_zotero_metadata_fixer_v2.py

# Test on one item
python 2026_05_21_zotero_metadata_fixer_v2.py --key ITEMKEY --apply

# Full run
python 2026_05_21_zotero_metadata_fixer_v2.py --apply
```

## Author

Christian Ortiz | https://github.com/ChristianOrtizL
