use super::order::Order;
use crate::util::text::normalize;

#[derive(Debug, Clone)]
pub struct User {
    pub name: String,
    pub orders: Vec<Order>,
}

impl User {
    pub fn new(name: &str) -> Self {
        User { name: normalize(name), orders: Vec::new() }
    }

    pub fn add_order(&mut self, order: Order) {
        self.orders.push(order);
    }

    pub fn total(&self) -> u32 {
        let mut sum = 0;
        for order in &self.orders {
            sum += order.amount();
        }
        sum
    }
}
