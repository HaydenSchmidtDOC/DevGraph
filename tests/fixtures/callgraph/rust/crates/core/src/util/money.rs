use self::fmt::cents_to_string;

pub fn format(cents: u32) -> String {
    cents_to_string(cents)
}

mod fmt {
    // super = util::money, super::super = util.
    pub fn cents_to_string(c: u32) -> String {
        super::super::text::normalize(&format!("${}.{:02}", c / 100, c % 100))
    }
}
