mod cli;
mod report;

use shop_core::models::user::User;
use shop_core::net::client::Client;

fn main() {
    let args = cli::parse_args();
    let client = Client::new(&args.base);
    let user = client.fetch_user(&args.name);
    let local = User::new("local");
    report::print(&user);
    report::print(&local);
    println!("core {}", shop_core::version());
}
