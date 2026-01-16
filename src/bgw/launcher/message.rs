use serde::{Deserialize, Serialize};

use crate::config::Config;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum ExtensionStatus {
    Exist,
    NoExtension,
    NoForeignServer,
}

#[derive(Debug, Serialize, Deserialize)]
pub enum LauncherMessage {
    DbExtensionStatus {
        db_oid: u32,
        status: ExtensionStatus,
    },
    NewConfig {
        db_oid: u32,
        config: Config,
    },
    Subscribe {
        db_oid: u32,
        subject: String,
        fn_name: String,
    },
    Unsubscribe {
        db_oid: u32,
        subject: String,
        fn_name: String,
    },
    SubscriberExit {
        db_oid: u32,
        reason: Result<(), String>,
    },
    ForeignServerDropped {
        db_oid: u32,
    },
    #[cfg(any(test, feature = "pg_test"))]
    ChangeStatus {
        db_oid: u32,
        master: bool,
    },
}
