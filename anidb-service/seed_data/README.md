# Seed Data Directory

This directory contains initial AniDB XML data for seeding the cache.

## Usage

1. Place your AniDB XML files as a zip archive in this directory
2. On first startup, if `/data` is empty, the service will automatically:
   - Detect the zip file
   - Extract all XML files to `/data`
   - Index them into the database

## File Format

- **Supported:** `.zip` files containing `.xml` files
- **Naming:** Any zip filename is acceptable (first found will be used)
- **Structure:** XML files can be in root or subdirectories within the zip

## Example

```
seed_data/
└── anidb_xmls.zip
    ├── 1.xml
    ├── 2.xml
    ├── 3.xml
    └── ...
```

## Notes

- Seed extraction only happens if `/data` is empty
- If `/data` already contains XML files, seed extraction is skipped
- Large archives may take several minutes to extract and index
- Progress is logged during indexing (every 100 files)
- Zip files are mounted read-only in Docker for safety
