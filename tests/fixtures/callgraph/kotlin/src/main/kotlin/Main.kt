package com.acme.notes

import com.acme.notes.data.NoteRepository
import com.acme.notes.export.MarkdownExporter
import com.acme.notes.report.runReport
import com.acme.notes.tags.TagSet
import com.acme.notes.util.slugify

fun main() {
    val repo = NoteRepository()
    val note = repo.create("Hello World")
    val tags = TagSet()
    tags.add("kotlin")
    println("Created ${ slugify(note.title) } at ${formatStamp(note.createdAt)}")
    println(MarkdownExporter().export(repo.all()))
    runReport(repo)
}
