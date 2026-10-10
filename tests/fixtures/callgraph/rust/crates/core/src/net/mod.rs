pub mod client;

use self::client::Client;

pub fn connect(base: &str) -> Client {
    self::client::Client::new(base)
}
