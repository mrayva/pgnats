use pgrx::pg_sys as sys;

use std::{ffi, ops::Deref, ptr::NonNull};

#[derive(Debug)]
pub struct DynamicSharedMemory {
    seg: NonNull<sys::dsm_segment>,
}

impl DynamicSharedMemory {
    pub fn new(size: usize) -> anyhow::Result<Self> {
        // SAFETY:
        // `dsm_create` return a pointer managed by Postgres.
        // The returned pointer is checked for null before being wrapped in `NonNull`.
        // Lifetime is tied to Postgres dynamic shared memory subsystem.
        let seg = unsafe { sys::dsm_create(size, 0) };

        NonNull::new(seg)
            .map(|seg| DynamicSharedMemory { seg })
            .ok_or_else(|| anyhow::anyhow!("Failed to create Dynamic Shared Memory"))
    }

    pub fn attach(handle: DsmHandle) -> anyhow::Result<Self> {
        // SAFETY:
        // `dsm_attach` return a pointer managed by Postgres.
        // The returned pointer is checked for null before being wrapped in `NonNull`.
        // Lifetime is tied to Postgres dynamic shared memory subsystem.
        let seg = unsafe { sys::dsm_attach(*handle) };
        NonNull::new(seg)
            .map(|seg| DynamicSharedMemory { seg })
            .ok_or_else(|| anyhow::anyhow!("Failed to attach to Dynamic Shared Memory"))
    }

    pub fn handle(&self) -> DsmHandle {
        // SAFETY:
        // `self.seg` is a valid pointer obtained from Postgres DSM API.
        // Postgres guarantees that the segment remains valid until detached.
        unsafe { sys::dsm_segment_handle(self.seg.as_ptr()).into() }
    }

    pub fn addr(&self) -> *mut ffi::c_void {
        // SAFETY:
        // `self.seg` is a valid pointer obtained from Postgres DSM API.
        // Postgres guarantees that the segment remains valid until detached.
        unsafe { sys::dsm_segment_address(self.seg.as_ptr()) }
    }

    pub fn as_ptr(&self) -> *mut sys::dsm_segment {
        self.seg.as_ptr()
    }
}

impl Drop for DynamicSharedMemory {
    fn drop(&mut self) {
        // SAFETY:
        // `self.seg` is a valid DSM segment pointer and is detached exactly once.
        // After this call the pointer must not be used again.
        unsafe { sys::dsm_detach(self.seg.as_ptr()) };
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct DsmHandle(pub u32);

impl From<u32> for DsmHandle {
    fn from(value: u32) -> Self {
        Self(value)
    }
}

impl From<DsmHandle> for u32 {
    fn from(value: DsmHandle) -> Self {
        value.0
    }
}

impl Deref for DsmHandle {
    type Target = u32;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
