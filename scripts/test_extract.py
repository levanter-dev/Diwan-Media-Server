"""Quick extraction test for a single adapter."""
import os, sys, logging, time, json
os.environ['EXPERIMENTAL'] = '1'
os.environ['MEDIA_DEBUG_LOGS'] = '1'
os.environ['SCRAPE_BATCH_ITEM_TIMEOUT'] = '30'

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(threadName)-12s] %(levelname)-5s %(message)s', datefmt='%H:%M:%S')
sys.path.insert(0, '.')

from app.scrapers import extract_servers

def test(adapter_id, source_id, title, media_type="movie"):
    print(f"\n=== Testing {adapter_id}: {title} ===")
    t0 = time.time()
    try:
        r = extract_servers(adapter_id, source_id, title, media_type)
        servers = r.get("servers", [])
        error = r.get("error")
        print(f"Result: servers={len(servers)}, error={error}, elapsed={time.time()-t0:.1f}s")
        for s in servers:
            url = s.get("video_url", "")
            print(f"  -> {url[:200]}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--adapter", "-a", required=True)
    p.add_argument("--source", "-s", required=True)
    p.add_argument("--title", "-t", default="The Invention of Lying")
    p.add_argument("--type", default="movie")
    args = p.parse_args()
    test(args.adapter, args.source, args.title, args.type)
