import arxiv

client = arxiv.Client(
    page_size=20,
    delay_seconds=3,
    num_retries=3
)

search = arxiv.Search(
    query='ti:"agent" OR abs:"agentic" OR abs:"LLM agent"',
    max_results=50,
    sort_by=arxiv.SortCriterion.SubmittedDate,
    sort_order=arxiv.SortOrder.Descending
)

for paper in client.results(search):
    print("=" * 80)
    print(paper.title)
    print(paper.published)
    print(paper.entry_id)
    print(paper.summary[:500])