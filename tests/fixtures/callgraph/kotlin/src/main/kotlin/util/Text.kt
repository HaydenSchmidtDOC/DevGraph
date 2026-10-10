package com.acme.notes.util

fun slugify(s: String): String = s.trim().lowercase().replace(' ', '-')

fun truncate(s: String, n: Int): String = if (s.length <= n) s else s.take(n) + "..."
