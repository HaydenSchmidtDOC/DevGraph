use shop_core::models::user::User;
use shop_core::util::money;
use std::collections::HashMap;

pub fn print(user: &User) {
    let mut seen: HashMap<String, u32> = HashMap::new();
    seen.insert(user.name.clone(), user.total());
    println!("{} owes {}", user.name, money::format(user.total()));
}
