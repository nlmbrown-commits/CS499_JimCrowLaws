# CS499_JimCrowLaws

Searchable public digital archive of Kentucky Jim Crow laws (1865–1966).

**Pipeline:** Scanned PDFs → OCR → Document Processing (LLM) → Database → Website

---

## Setup

All API keys live in a single `.env` file in the project root. Copy the template and fill in your keys:

```bash
cp .env_template .env
# then edit .env with your keys
```

---

## OCR Component (`ocr/`)

Install dependencies:
```bash
pip install "llama-cloud>=1.0" pypdf python-dotenv
```

Run (opens a file picker to select a PDF):
```bash
python3 ocr/jim_crow_ocr.py
```

Output: `ocr_results/{filename}_results.json`

### Expected output format (v2)

The document processing component expects OCR output in this format.

```json
{
  "source": {
    "filename": "Gen_assemb_v1-1865.pdf",
    "title": "Acts Passed at the Session of the General Assembly of Kentucky",
    "author": "Kentucky General Assembly",
    "year": 1865,
    "document_type": null
  },
  "pages": [
    {
      "page_number": 87,
      "text": "# CHAPTER 399\n\nAN ACT to establish separate schools...",
      "keyword_hits": ["colored", "negro"]
    }
  ],
  "keyword_references": [
    {
      "keyword": "colored",
      "page_number": 87,
      "line_number_in_page": 3,
      "context": "...surrounding lines..."
    }
  ],
  "statistics": {
    "total_pages": 450,
    "pages_with_keyword_hits": 27,
    "total_keyword_hits": 34
  },
  "ocr_metadata": {
    "engine": "llama_cloud",
    "result_type": "markdown",
    "processed_at": "2026-02-19T10:00:00Z",
    "format_version": "2.0"
  }
}
```

See `doc_processing/sample_input/toy_ocr_output.json` for a full working example.

---

## Document Processing Component (`doc_processing/`)

Takes OCR output and classifies each statute section as a Jim Crow law, extracting structured metadata for the database.

### Install dependencies

Install the core packages plus the package for your chosen LLM provider:

```bash
pip3 install llama-index python-dotenv

# Pick one:
pip3 install llama-index-llms-anthropic   # Anthropic Claude (default)
pip3 install llama-index-llms-openai      # OpenAI GPT
pip3 install llama-index-llms-ollama      # Ollama (local/free)
pip3 install llama-index-llms-openai-like # Groq, Together AI, LM Studio, etc.
```

### Configure

In `.env`, set `LLM_PROVIDER` and the matching API key. See `.env_template` for all options.

### Run

```bash
# Against the built-in toy example (no OCR file needed):
python3 doc_processing/processor.py

# Against a real OCR output file:
python3 doc_processing/processor.py ocr_results/my_file_results.json
```

Output: `doc_processing_results/{filename}_classified.json`

### Output format

```json
{
  "source_document": { "filename": "...", "year": 1865, "document_type": "session_laws" },
  "entries": [
    {
      "entry_id": "gen_assemb_1865_p87_s1",
      "page_number": 87,
      "citation": "Acts of KY General Assembly, 1865, p. 87",
      "ocr_text": "...",
      "classification": {
        "is_jim_crow": "yes",
        "confidence": 0.95,
        "category": "education",
        "summary": "Mandates separate schools for white and colored children.",
        "racial_indicator": "explicit",
        "needs_human_review": false
      }
    }
  ],
  "human_review_queue": [ ... ],
  "statistics": { "total_sections": 24, "classified_jim_crow": 3, ... }
}
```

`document_type` is inferred by the LLM from the document text and stored once on `source_document` (not per entry). Valid values: `session_laws`, `constitution`, `state_constitution`, `amendments`, `codes`, `criminal_laws`, `civil_laws`, `other`. Reviewers can correct it in the UI.

Entries flagged `needs_human_review: true` (ambiguous cases or confidence < 0.7) are also listed separately in `human_review_queue`.

---

## Law Extraction (`doc_processing/extract_laws.py`)

The processor classifies one **page** at a time, so a five-page act counts as five entries and a page shared by two acts is credited to both. `extract_laws.py` regroups the reviewed page-level results into complete **laws**.

```bash
python3 doc_processing/extract_laws.py            # no dependencies beyond the standard library
python3 doc_processing/extract_laws.py --verify   # also re-classify each law's full text with the LLM
```

How it works:

- **Unit of a law.** Session-law volumes are split into acts (`CHAPTER 64.` / `AN ACT ...` through `Approved March 18, 1908.`). Compiled codes and school-law compilations are split into numbered sections (`§ 4363.`), the way they are cited. Volumes with neither structure (e.g. the Judicial Dictionary) keep one law per page.
- **Shared pages.** When a flagged page holds more than one law, the finding goes to the law that uses racial language on that page; for implicit pages, to the law whose text best matches the page's LLM title and keywords.
- **Index and contents pages** are set aside, since they list acts rather than contain them.
- **Reviewer work carries over**: `reviewed`, `reviewer_note` and corrected categories from the page entries are kept on each law.
- `key_provisions` holds the sections of the law, on its flagged pages, that use racial language, so a 150-page act still points to its segregation clause.
- **Editions are grouped into statutes.** The same law reprinted in several compilations (e.g. Ky. Stat. § 4477 in the 1903, 1909, 1915 and 1922 editions and the school-law compilations) shares a `statute_id`. Printings are linked by the same Kentucky Statutes § number (including a compilation's "(§ 4477 Ky. St.)" reference) or by near-identical opening text in different volumes. Session-law acts are separate enactments and are never merged with one another. Two laws printed in the same volume are never merged on text alone, because the charters for each class of city carry near-identical "separate schools" sections that are distinct laws; such pairs are listed in `possible_matches.csv` for a person to decide.
- **Titles are cleaned.** OCR word boundaries are repaired ("Be itenactedby theGeneral Assembly" → "Be it enacted by the General Assembly", "Com- monwealth" → "Commonwealth") using `wordninja` plus a vocabulary of Kentucky place names and legal terms learned from the LLM's page summaries. Words it cannot split confidently are left as scanned, and `title_raw` keeps the original. Install with `pip install wordninja`; without it only punctuation is fixed.
- Laws are flagged `needs_review` when the attribution relied on title matching, the act's end wasn't found, the act runs over 40 pages, or (with `--verify`) the LLM does not confirm it on the full text. Identical laws (pages repeated in a scan) are marked with `duplicate_of`.

Output in `law_results/`: `laws.json` and `laws.csv` (one row per law, with citation, page range, flagged pages, category, full text and provenance), `statutes.csv` (one row per distinct statute), `possible_matches.csv`, `page_to_law.csv` (each flagged page → its law) and `excluded_pages.csv`.

`python3 doc_processing/export_laws_xlsx.py` (needs `openpyxl`) writes all of it to `law_results/jim_crow_laws.xlsx`, one sheet per view plus a summary.

---

## Reviewer UI (`reviewer/`)

A Gradio web app for human reviewers to review, correct, and sign off on LLM classifications before the data goes into the archive database.

### Install

```bash
pip install gradio
```

### Run

```bash
python reviewer/reviewer_ui.py
```

Opens at `http://localhost:7860` in your browser.

### What it does

- Loads any classified JSON file from `doc_processing_results/`
- Shows entries in priority order: flagged (`needs_human_review`) first, then `is_jim_crow=yes`, then the rest
- Lets you correct `is_jim_crow` (yes / no / ambiguous) and `category` per entry, leave a free-text note, and save
- Lets you set the **document type** once per document (stored on `source_document`) via a dedicated selector and "Save Document Type" button; selecting "other" reveals a free-text field for custom types
- Both `category` and `document_type` accept custom free-text values via an "other" option
- Saves corrections back to the same JSON file immediately on each save click
- Tracks progress with a live counter: overall entries reviewed and priority entries remaining

### What "reviewed" means for a file

A file is considered **complete** when all **priority entries** have been saved — that is, every entry where `is_jim_crow = "yes"` or `needs_human_review = true`. The UI shows a green "✅ All priority entries reviewed" banner when this threshold is reached.
