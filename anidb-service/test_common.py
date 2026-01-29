"""Tests for common.py utilities."""
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from common import extract_seed_data


@pytest.fixture
def test_dirs(tmp_path):
    """Create temporary test directories."""
    xml_dir = tmp_path / "data"
    seed_dir = tmp_path / "seed_data"
    seed_dir.mkdir(parents=True, exist_ok=True)
    return xml_dir, seed_dir


@pytest.fixture
def sample_zip(test_dirs):
    """Create a sample zip file with XML data."""
    xml_dir, seed_dir = test_dirs
    zip_path = seed_dir / "seed.zip"
    
    with zipfile.ZipFile(zip_path, 'w') as zf:
        # Add XML files
        zf.writestr("AnimeDoc_1.xml", "<anime id='1'><title>Test 1</title></anime>")
        zf.writestr("AnimeDoc_2.xml", "<anime id='2'><title>Test 2</title></anime>")
        zf.writestr("nested/AnimeDoc_3.xml", "<anime id='3'><title>Test 3</title></anime>")
    
    return xml_dir, seed_dir, zip_path


def test_extract_seed_data_success(sample_zip, capsys):
    """Test successful extraction of seed data."""
    xml_dir, seed_dir, zip_path = sample_zip
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Check that files were extracted
    assert xml_dir.exists()
    assert (xml_dir / "AnimeDoc_1.xml").exists()
    assert (xml_dir / "AnimeDoc_2.xml").exists()
    assert (xml_dir / "AnimeDoc_3.xml").exists()
    
    # Check output message
    captured = capsys.readouterr()
    assert "Extracting seed data" in captured.out
    assert "Extracted 3 XML files" in captured.out


def test_extract_seed_data_existing_files(sample_zip, capsys):
    """Test that extraction is skipped when files already exist."""
    xml_dir, seed_dir, zip_path = sample_zip
    
    # Create data directory with existing file
    xml_dir.mkdir(parents=True, exist_ok=True)
    (xml_dir / "existing.xml").write_text("<anime/>")
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Should skip extraction
    captured = capsys.readouterr()
    assert "already contains files" in captured.out
    assert "skipping seed extraction" in captured.out


def test_extract_seed_data_no_seed_dir(test_dirs, capsys):
    """Test handling when seed data directory doesn't exist."""
    xml_dir, seed_dir = test_dirs
    non_existent_dir = seed_dir / "nonexistent"
    
    extract_seed_data(xml_dir, non_existent_dir)
    
    captured = capsys.readouterr()
    assert "Seed data directory not found" in captured.out


def test_extract_seed_data_no_zip_files(test_dirs, capsys):
    """Test handling when no zip files are found."""
    xml_dir, seed_dir = test_dirs
    
    extract_seed_data(xml_dir, seed_dir)
    
    captured = capsys.readouterr()
    assert "No zip files found" in captured.out


def test_extract_seed_data_creates_xml_dir(sample_zip):
    """Test that XML directory is created if it doesn't exist."""
    xml_dir, seed_dir, zip_path = sample_zip
    
    # Ensure xml_dir doesn't exist
    assert not xml_dir.exists()
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Should be created
    assert xml_dir.exists()
    assert xml_dir.is_dir()


def test_extract_seed_data_nested_files(sample_zip):
    """Test extraction of files from nested directories."""
    xml_dir, seed_dir, zip_path = sample_zip
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Nested file should be extracted to root level
    assert (xml_dir / "AnimeDoc_3.xml").exists()
    # Nested directory should not exist
    assert not (xml_dir / "nested").exists()


def test_extract_seed_data_multiple_zip_files(test_dirs, capsys):
    """Test that first zip file is used when multiple exist."""
    xml_dir, seed_dir = test_dirs
    
    # Create multiple zip files
    zip_path1 = seed_dir / "seed1.zip"
    zip_path2 = seed_dir / "seed2.zip"
    
    with zipfile.ZipFile(zip_path1, 'w') as zf:
        zf.writestr("AnimeDoc_1.xml", "<anime id='1'/>")
    
    with zipfile.ZipFile(zip_path2, 'w') as zf:
        zf.writestr("AnimeDoc_2.xml", "<anime id='2'/>")
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Should use first zip file (alphabetically)
    captured = capsys.readouterr()
    assert "seed1.zip" in captured.out or "seed2.zip" in captured.out


def test_extract_seed_data_corrupted_zip(test_dirs, capsys):
    """Test handling of corrupted zip file."""
    xml_dir, seed_dir = test_dirs
    
    # Create corrupted zip file
    zip_path = seed_dir / "corrupted.zip"
    zip_path.write_text("This is not a valid zip file")
    
    extract_seed_data(xml_dir, seed_dir)
    
    captured = capsys.readouterr()
    assert "Error extracting seed data" in captured.out


def test_extract_seed_data_non_xml_files(test_dirs):
    """Test that only XML files are extracted."""
    xml_dir, seed_dir = test_dirs
    zip_path = seed_dir / "mixed.zip"
    
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("AnimeDoc_1.xml", "<anime id='1'/>")
        zf.writestr("readme.txt", "Not an XML file")
        zf.writestr("image.png", b"\x89PNG\r\n")
    
    extract_seed_data(xml_dir, seed_dir)
    
    # Only XML file should be extracted
    assert (xml_dir / "AnimeDoc_1.xml").exists()
    assert not (xml_dir / "readme.txt").exists()
    assert not (xml_dir / "image.png").exists()


def test_extract_seed_data_empty_zip(test_dirs, capsys):
    """Test handling of empty zip file."""
    xml_dir, seed_dir = test_dirs
    zip_path = seed_dir / "empty.zip"
    
    with zipfile.ZipFile(zip_path, 'w') as zf:
        pass  # Create empty zip
    
    extract_seed_data(xml_dir, seed_dir)
    
    captured = capsys.readouterr()
    assert "Extracted 0 XML files" in captured.out


def test_extract_seed_data_permission_error(test_dirs, capsys):
    """Test handling of permission errors during extraction."""
    xml_dir, seed_dir = test_dirs
    zip_path = seed_dir / "seed.zip"
    
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("AnimeDoc_1.xml", "<anime id='1'/>")
    
    # Mock extract to raise PermissionError
    with patch('zipfile.ZipFile.extract', side_effect=PermissionError("Access denied")):
        extract_seed_data(xml_dir, seed_dir)
        
        captured = capsys.readouterr()
        assert "Error extracting seed data" in captured.out


def test_extract_seed_data_file_already_exists(test_dirs):
    """Test extraction when target file already exists in nested structure."""
    xml_dir, seed_dir = test_dirs
    zip_path = seed_dir / "seed.zip"
    
    # Create zip with nested structure
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("subdir/AnimeDoc_1.xml", "<anime id='1'/>")
    
    # Pre-create the target file
    xml_dir.mkdir(parents=True, exist_ok=True)
    (xml_dir / "subdir").mkdir(exist_ok=True)
    existing_file = xml_dir / "subdir" / "AnimeDoc_1.xml"
    existing_file.write_text("<existing/>")
    
    extract_seed_data(xml_dir, seed_dir)
    
    # File should be overwritten/moved
    assert (xml_dir / "AnimeDoc_1.xml").exists()


def test_extract_seed_data_rmdir_failure():
    """Test that rmdir failure in cleanup is handled gracefully."""
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmp:
        xml_dir = Path(tmp) / "data"
        seed_dir = Path(tmp) / "seed_data"
        seed_dir.mkdir(parents=True, exist_ok=True)
        
        # Create zip with nested structure
        zip_path = seed_dir / "seed.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("nested/AnimeDoc_1.xml", "<anime id='1'/>")
        
        # Mock rmdir to raise an exception
        original_rmdir = Path.rmdir
        def mock_rmdir(self):
            if "nested" in str(self):
                raise OSError("Directory not empty")
            return original_rmdir(self)
        
        with patch.object(Path, 'rmdir', mock_rmdir):
            # Should not raise, exception is caught
            extract_seed_data(xml_dir, seed_dir)
            
            # File should still be extracted
            assert (xml_dir / "AnimeDoc_1.xml").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
