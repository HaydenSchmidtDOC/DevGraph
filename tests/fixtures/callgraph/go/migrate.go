package main

import "example.com/shop/v2/internal/legacydb"

// The legacydb directory declares `package db`, so it is referred to as db.
func migrate() {
	conn := db.Open("file:shop.db")
	defer conn.Close()
}
