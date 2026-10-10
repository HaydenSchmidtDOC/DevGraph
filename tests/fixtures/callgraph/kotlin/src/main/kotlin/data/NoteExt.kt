package com.acme.notes.data

fun Note.isStale(now: Long): Boolean = now - createdAt > 86_400_000
