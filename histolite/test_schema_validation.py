import os
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'rootfs', 'opt', 'histolite'))

from database import HaDatabase, SchemaUnrecognizedError


def _make_db_with_legacy_schema():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE states (entity_id TEXT, state TEXT, last_updated TEXT, attributes_id INTEGER)")
    conn.execute("CREATE TABLE states_meta (entity_id TEXT, metadata_id INTEGER)")
    conn.commit()
    conn.close()
    return path


def _make_db_with_modern_schema():
    """Schema moderno reale: states_meta + states.metadata_id, con la colonna
    vestigiale states.entity_id ancora presente (nullable) come fa HA."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE states ("
        "state_id INTEGER PRIMARY KEY, entity_id TEXT, state TEXT, "
        "metadata_id INTEGER, last_updated_ts REAL, last_reported_ts REAL, "
        "attributes_id INTEGER, context_id_bin BLOB)"
    )
    conn.execute("CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
    conn.commit()
    conn.close()
    return path


def test_modern_schema_with_vestigial_entity_id_is_supported():
    db_path = _make_db_with_modern_schema()
    try:
        db = HaDatabase(db_path)
        schema = db.get_schema_info()
        assert schema["schema_type"] == "modern", schema["schema_type"]
        db.validate_supported_backend_and_schema()
    finally:
        os.remove(db_path)


def test_legacy_schema_raises_on_validation():
    db_path = _make_db_with_legacy_schema()
    try:
        db = HaDatabase(db_path)
        schema = db.get_schema_info()
        assert schema["schema_type"] == "unsupported_legacy"
        try:
            db.validate_supported_backend_and_schema()
            raise AssertionError("Expected SchemaUnrecognizedError")
        except SchemaUnrecognizedError:
            pass
    finally:
        os.remove(db_path)


if __name__ == "__main__":
    test_modern_schema_with_vestigial_entity_id_is_supported()
    test_legacy_schema_raises_on_validation()
    print("schema validation ok")
