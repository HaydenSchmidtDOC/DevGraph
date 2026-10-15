use shop_core::models::user::User;
use shop_core::util::money;
use shop_core::{format_money, normalize, Client};
use std::collections::HashMap;

pub fn print(user: &User) {
    let mut seen: HashMap<String, u32> = HashMap::new();
    seen.insert(user.name.clone(), user.total());
    println!("{} owes {}", user.name, money::format(user.total()));
}

pub fn summary(name: &str) -> String {
    let client = Client::new(name);
    let user = client.fetch_user(name);
    normalize(&format_money(user.total()))
}
