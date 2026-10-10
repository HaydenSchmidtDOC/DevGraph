use std::env;

pub struct Args {
    pub base: String,
    pub name: String,
}

pub fn parse_args() -> Args {
    let mut it = env::args().skip(1);
    let base = it.next();
    let name = it.next();
    Args { base: base.unwrap_or(default_base()), name: name.unwrap_or_default() }
}

fn default_base() -> String {
    String::from("http://localhost")
}
