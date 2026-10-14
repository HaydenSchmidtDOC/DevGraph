// An integration test is its own crate: shop_core is reached by name, not crate::.
use shop_core::models::order::Order;
use shop_core::models::user::User;

#[test]
fn totals_orders() {
    let mut user = User::new("Ada");
    user.add_order(Order::new(250, 2));
    assert_eq!(user.total(), 500);
}
