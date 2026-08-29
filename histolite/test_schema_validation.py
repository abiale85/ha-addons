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
    test_legacy_schema_raises_on_validation()
    print("schema validation ok")
