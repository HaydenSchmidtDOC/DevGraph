package com.acme.notes.data

data class Note(val id: Int, val title: String, val createdAt: Long) {
    fun summary() = "$id: ${title.take(20)}"
}
