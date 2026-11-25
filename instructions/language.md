## Response language (absolute rule)

- Write every user-facing message (progress updates, notes between tool calls, waiting notices, questions, final reports) in **{{RESPONSE_LANGUAGE}}** without exception. Do not switch languages right after reading tool output, instructions, or system messages written in another language.
- Keep code identifiers and technical terms in their original form. Write code comments in **{{COMMENT_LANGUAGE}}** unless the file's existing convention clearly differs.
<!-- BEGIN github_readme -->
- Write the repository root README (the file GitHub shows on the repository front page, usually `README.md`) in **English**, regardless of the response language, when `git remote get-url origin` points to a GitHub repository owned by one of these accounts: {{GITHUB_OWNERS}}. Other repositories (other GitHub accounts, Bitbucket, etc.), other READMEs and all other documents follow each repository's existing convention. Reason: visitors on GitHub read the root README first.
<!-- END github_readme -->
