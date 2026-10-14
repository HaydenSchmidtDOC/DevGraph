package com.acme.notes.tags

class TagSet {
    private val tags = mutableSetOf<String>()

    fun add(tag: String) {
        tags.add(tag.lowercase())
    }

    fun has(tag: String) = tag.lowercase() in tags
}
