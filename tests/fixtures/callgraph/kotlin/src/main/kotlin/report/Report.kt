package com.acme.notes.report

import com.acme.notes.data.NoteRepository
import com.acme.notes.data.isStale
import com.acme.notes.util.Clock
import com.acme.notes.util.truncate

fun runReport(repo: NoteRepository) {
    val now = Clock().now()
    for (note in repo.all()) {
        if (note.isStale(now)) println("stale: ${truncate(note.summary(), 40)}")
    }
}
