"""Check the existing public DuckDuckGo HTML adapter with one generic query."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_service import FirecrawlKeylessProvider


if __name__ == '__main__':
    result=FirecrawlKeylessProvider().search(
        'Python asyncio task cancellation official documentation',limit=5)
    print(json.dumps({'provider':'firecrawl_keyless','result_count':len(result.get('results',[])),
        'results':[{'title':row.get('title'),'url':row.get('url')} for row in result.get('results',[])]},
        ensure_ascii=False,indent=2))
