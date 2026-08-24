File Matching Convention

- Raw file naming (Step 1): Use a common prefix with the entity, year and type.
	Example: BHP_2026_NOM.pdf -> produces BHP_2026_NOM.json

- Annual report naming and location (Step 2): Save the Annual Report PDF using the
	same prefix and an _AR suffix under the data/annual_reports/raw/ directory.
	Example: data/annual_reports/raw/BHP_2026_AR.pdf

- General rules:
	- Prefix: ENTITY_YEAR (e.g., BHP_2026)
	- Suffix: document type (e.g., NOM, AR, FIN, SUP)
	- Extensions: raw files as .pdf, generated metadata as .json
	- Keep file names ASCII-safe, use underscores, no spaces.

- Mapping examples:
	- BHP_2026_NOM.pdf  -> BHP_2026_NOM.json
	- BHP_2026_AR.pdf   -> data/annual_reports/raw/BHP_2026_AR.pdf
	- BHP_2026_FIN.pdf  -> BHP_2026_FIN.json

Ensure consistency across steps so automated pipelines can match raw PDFs to their
generated JSON metadata by filename prefix and suffix.
