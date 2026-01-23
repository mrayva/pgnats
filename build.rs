use std::{collections::BTreeSet, path::PathBuf};

use anyhow::Context;

fn main() -> anyhow::Result<()> {
    let manifest_dir =
        std::env::var("CARGO_MANIFEST_DIR").context("CARGO_MANIFEST_DIR is missing")?;
    let cargo_toml_path = PathBuf::from(&manifest_dir).join("Cargo.toml");

    let cargo_toml_content =
        std::fs::read_to_string(&cargo_toml_path).context("Failed to read project's Cargo.toml")?;

    let mut manifest =
        cargo_toml::Manifest::from_str(&cargo_toml_content).context("Cargo.toml is invalid")?;

    let pgrx_version = manifest
        .dependencies
        .get_mut("pgrx")
        .and_then(|dep| dep.try_detail_mut().ok())
        .and_then(|dep| dep.version.as_ref())
        .context("pgrx dependency not found in Cargo.toml")?;

    let version_file = PathBuf::from(&manifest_dir).join(".cargo-pgrx-version");
    std::fs::write(&version_file, pgrx_version)
        .context("Failed to write .cargo-pgrx-version file")?;

    println!("cargo:rerun-if-changed={}", cargo_toml_path.display());

    shadow_rs::ShadowBuilder::builder()
        .deny_const(BTreeSet::from_iter([
            shadow_rs::BUILD_OS,
            shadow_rs::CARGO_METADATA,
            shadow_rs::CARGO_TREE,
            shadow_rs::CARGO_CLIPPY_ALLOW_ALL,
            shadow_rs::CARGO_MANIFEST_DIR,
            shadow_rs::CARGO_VERSION,
            shadow_rs::BUILD_TARGET,
            shadow_rs::BUILD_TARGET_ARCH,
            shadow_rs::PKG_DESCRIPTION,
            shadow_rs::PKG_VERSION_MAJOR,
            shadow_rs::PKG_VERSION_MINOR,
            shadow_rs::PKG_VERSION_PATCH,
            shadow_rs::PKG_VERSION_PRE,
        ]))
        .build()
        .context("Failed to fetch crate info")?;

    Ok(())
}
