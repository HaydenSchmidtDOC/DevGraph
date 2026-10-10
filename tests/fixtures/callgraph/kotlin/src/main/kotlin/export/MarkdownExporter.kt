package com.acme.notes.export

import com.acme.notes.data.Note
import com.acme.notes.util.*

class MarkdownExporter {
    fun export(notes: List<Note>): String {
        val sb = StringBuilder()
        for (n in notes) sb.append(line(n))
        return sb.toString()
    }

    private fun line(n: Note) = "- ${truncate(n.title, 30)} (${slugify(n.title)})\n"
}
