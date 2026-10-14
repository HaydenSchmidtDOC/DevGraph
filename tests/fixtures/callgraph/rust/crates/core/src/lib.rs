pub mod models;
pub mod net;
pub mod util;

// The crate root's facade: a type re-exported through net (which re-exports it
// from net::client), every item of util::text, and a function under another name.
pub use net::Client;
pub use util::text::*;
pub use util::money::format as format_money;

pub fn version() -> &'static str {
    "0.1.0"
}
