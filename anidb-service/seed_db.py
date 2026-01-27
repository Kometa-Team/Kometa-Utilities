import os
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import aiosqlite

from common import extract_seed_data

# Path to your XML files and the database
XML_DIR = Path(os.getenv("XML_DIR", "./data"))
DB_PATH = Path(os.getenv("DB_PATH", "./database.db"))
SEED_DATA_DIR = Path(os.getenv("SEED_DATA_DIR", "./seed_data"))


async def index_xml(aid: str, xml_text: str, db: aiosqlite.Connection) -> None:
    """Index a single XML file into the database."""
    try:
        root = ET.fromstring(xml_text)
        
        # Clear old metadata
        await db.execute("DELETE FROM tags WHERE aid = ?", (aid,))
        await db.execute("DELETE FROM relations WHERE aid = ?", (aid,))
        
        # Index Tags
        tags = [
            (aid, t.findtext("name"), int(t.get("weight", 0)))
            for t in root.findall(".//tag")
            if t.findtext("name")
        ]
        if tags:
            await db.executemany("INSERT INTO tags VALUES (?, ?, ?)", tags)
        
        # Index Relations
        rels = [
            (aid, int(r.get("id") or "0"), r.get("type") or "")
            for r in root.findall(".//relatedanime")
            if r.get("id") and r.get("type")
        ]
        if rels:
            await db.executemany("INSERT INTO relations VALUES (?, ?, ?)", rels)
        
        # Update Master Record
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)",
            (aid, datetime.now().isoformat())
        )
        
        print(f"✅ Indexed AID: {aid}")
        
    except ET.ParseError as e:
        print(f"❌ XML Parse Error for AID {aid}: {e}")
    except ValueError as e:
        print(f"❌ Data Conversion Error for AID {aid}: {e}")
    except Exception as e:
        print(f"❌ Error indexing AID {aid}: {e}")


async def main() -> None:
    """Main indexing function."""
    # Extract seed data from zip if needed
    extract_seed_data(XML_DIR, SEED_DATA_DIR)
    
    if not XML_DIR.exists():
        print(f"❌ Error: XML directory does not exist: {XML_DIR}")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Ensure tables exist
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS anime (
                aid INTEGER PRIMARY KEY,
                last_updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tags (
                aid INTEGER NOT NULL,
                name TEXT NOT NULL,
                weight INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS relations (
                aid INTEGER NOT NULL,
                related_aid INTEGER NOT NULL,
                type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_logs (
                timestamp TEXT NOT NULL,
                aid INTEGER,
                success INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_tags_aid ON tags(aid);
            CREATE INDEX IF NOT EXISTS idx_relations_aid ON relations(aid);
            CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp);
        ''')
        await db.commit()
        
        # Find all XML files
        files = list(XML_DIR.glob("*.xml"))
        
        if not files:
            print(f"⚠️ No XML files found in {XML_DIR}")
            return
        
        print(f"📚 Found {len(files)} files to index...")
        
        indexed = 0
        failed = 0
        
        for xml_file in files:
            aid = xml_file.stem.split("_")[1]  # Get filename without extension
            
            try:
                xml_text = xml_file.read_text(encoding="utf-8")
                await index_xml(aid, xml_text, db)
                indexed += 1
            except Exception as e:
                print(f"❌ Failed to read {xml_file}: {e}")
                failed += 1
            
            # Commit periodically
            if indexed % 100 == 0:
                await db.commit()
                print(f"💾 Progress: {indexed}/{len(files)} indexed...")
        
        await db.commit()
        
        print(f"\n{'='*50}")
        print(f"✅ Bulk indexing complete!")
        print(f"   Indexed: {indexed}")
        print(f"   Failed:  {failed}")
        print(f"   Total:   {len(files)}")
        print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())