package com.acme.notes.data

import com.acme.notes.util.Clock

class NoteRepository(private val clock: Clock = Clock()) {
    private val notes = mutableListOf<Note>()

    fun create(title: String): Note {
        val note = Note(nextId(), title, clock.now())
        notes.add(note)
        return note
    }

    fun all(): List<Note> = notes.toList()

    fun find(id: Int): Note? = notes.firstOrNull { it.id == id }

    private fun nextId() = notes.size + 1
}
