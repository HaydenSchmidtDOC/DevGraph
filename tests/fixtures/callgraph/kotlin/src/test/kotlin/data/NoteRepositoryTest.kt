package com.acme.notes.data

import kotlin.test.Test
import kotlin.test.assertEquals

class NoteRepositoryTest {
    @Test
    fun createsSequentialIds() {
        val repo = NoteRepository()
        val a = repo.create("a")
        val b = repo.create("b")
        assertEquals(a.id + 1, b.id)
    }
}
