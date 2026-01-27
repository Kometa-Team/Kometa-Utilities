"""Common utilities shared between main.py and seed_db.py."""
import os
import zipfile
from pathlib import Path


def extract_seed_data(xml_dir: Path, seed_data_dir: Path) -> None:
    """Extract seed data from zip file if data directory is empty.
    
    Args:
        xml_dir: Directory where XML files should be extracted
        seed_data_dir: Directory containing seed data zip files
    """
    # Check if data directory is empty
    if xml_dir.exists() and any(xml_dir.glob("*.xml")):
        print("📂 Data directory already contains files, skipping seed extraction")
        return
    
    # Look for zip file in seed_data directory
    if not seed_data_dir.exists():
        print("⚠️ Seed data directory not found, skipping seed extraction")
        return
    
    zip_files = list(seed_data_dir.glob("*.zip"))
    if not zip_files:
        print("⚠️ No zip files found in seed_data directory")
        return
    
    # Use the first zip file found
    zip_file = zip_files[0]
    print(f"📦 Extracting seed data from {zip_file.name}...")
    
    try:
        # Create data directory if it doesn't exist
        xml_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            # Extract all XML files to data directory
            xml_files = [f for f in zip_ref.namelist() if f.endswith('.xml')]
            for xml_file in xml_files:
                # Extract to data directory
                zip_ref.extract(xml_file, xml_dir)
                # Move from subdirectory if needed
                extracted_path = xml_dir / xml_file
                if extracted_path.parent != xml_dir:
                    final_path = xml_dir / extracted_path.name
                    extracted_path.rename(final_path)
                    # Clean up empty subdirectories
                    try:
                        extracted_path.parent.rmdir()
                    except:
                        pass
            
            print(f"✅ Extracted {len(xml_files)} XML files to {xml_dir}")
    except Exception as e:
        print(f"❌ Error extracting seed data: {e}")
