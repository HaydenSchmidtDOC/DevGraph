#[derive(Debug, Clone)]
pub struct Order {
    pub cents: u32,
    pub qty: u32,
}

impl Order {
    pub fn new(cents: u32, qty: u32) -> Self {
        Order { cents, qty }
    }

    pub fn amount(&self) -> u32 {
        self.cents * self.qty
    }
}
