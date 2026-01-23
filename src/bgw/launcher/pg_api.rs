use std::ptr::null_mut;

use pgrx::pg_sys as sys;

pub fn fetch_database_oids() -> Vec<sys::Oid> {
    // SAFETY:
    // 1. Relation is opened with AccessShareLock before scanning.
    // 2. Scan lifecycle strictly follows Postgres API contract:
    //    table_open -> table_beginscan_catalog -> heap_getnext* -> table_endscan -> table_close.
    // 3. Returned tuples remain valid until the scan is finished.
    unsafe {
        let mut workers = vec![];

        let rel = sys::table_open(sys::DatabaseRelationId, sys::AccessShareLock as _);

        let scan = sys::table_beginscan_catalog(rel, 0, null_mut());

        let mut tup = sys::heap_getnext(scan, sys::ScanDirection::ForwardScanDirection);

        while !tup.is_null() {
            let pgdb = &*(sys::GETSTRUCT(tup) as sys::Form_pg_database);

            if pgdb.datallowconn && !pgdb.datistemplate {
                workers.push(pgdb.oid);
            }

            tup = sys::heap_getnext(scan, sys::ScanDirection::ForwardScanDirection);
        }

        sys::table_endscan(scan);
        sys::table_close(rel, sys::AccessShareLock as _);

        workers
    }
}
