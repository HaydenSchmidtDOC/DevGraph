package com.acme.notes.data

data class Note(val id: Int, val title: String, val createdAt: Long) {
    fun summary() = "$id: ${title.take(20)}"

    fun asDraft() = Note.draft(title)

    // An anonymous companion: Kotlin names it Companion, and its members
    // are called on the class (`Note.draft(...)`).
    companion object {
        fun draft(title: String): Note = Note(0, normalizeTitle(title), 0L)

        private fun normalizeTitle(title: String) = title.trim()
    }
}
