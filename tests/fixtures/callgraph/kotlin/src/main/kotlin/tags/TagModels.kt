package com.acme.notes.tags

data class Tag(val name: String) { fun label() = "#$name" }
internal class TagIndex { fun size() = 0 }
