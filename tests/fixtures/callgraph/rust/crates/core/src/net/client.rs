use crate::models::user::User;

pub struct Client {
    base: String,
}

impl Client {
    pub fn new(base: &str) -> Self {
        Client { base: base.to_string() }
    }

    pub fn fetch_user(&self, name: &str) -> User {
        let url = self.url(name);
        User::new(&url)
    }

    fn url(&self, path: &str) -> String {
        format!("{}/{}", self.base, path)
    }
}
