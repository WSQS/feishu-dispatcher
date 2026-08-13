package dev.sopho.fdx.client.tree

import dev.sopho.fdx.client.network.TreeChildrenEntry
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TreeStateTest {
    private fun dir(name: String, path: String) = TreeChildrenEntry(name = name, path = path, type = "directory")

    private fun file(name: String, path: String) = TreeChildrenEntry(name = name, path = path, type = "file")

    @Test
    fun `root_shows_children_in_server_order`() {
        val state = TreeState().setChildren(
            ROOT_PATH,
            listOf(file("b.txt", "b.txt"), dir("a", "a"), file("c.txt", "c.txt")),
        )

        val rows = state.visibleRows()

        assertEquals(listOf("b.txt", "a", "c.txt"), rows.map { it.path })
        assertEquals(listOf(0, 0, 0), rows.map { it.depth })
        assertUniquePaths(rows)
    }

    @Test
    fun `toggle_cold_directory_expands_with_loading_placeholder`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a")))
            .toggle("a")

        assertTrue("a" in state.expandedPaths)
        assertTrue(state.directories.getValue("a").loading)
        val rows = state.visibleRows()
        assertEquals(listOf("a"), rows.map { it.path })
        assertTrue(rows.single().loading)
        assertNull(rows.single().error)
        assertUniquePaths(rows)
    }

    @Test
    fun `loaded_children_insert_after_directory_in_server_order`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a"), file("f.txt", "f.txt")))
            .toggle("a")
            .setChildren("a", listOf(file("a/x.txt", "a/x.txt"), dir("a/sub", "a/sub"), file("a/z.txt", "a/z.txt")))

        val rows = state.visibleRows()
        assertEquals(listOf("a", "a/x.txt", "a/sub", "a/z.txt", "f.txt"), rows.map { it.path })
        assertEquals(listOf(0, 1, 1, 1, 0), rows.map { it.depth })
        assertUniquePaths(rows)
    }

    @Test
    fun `collapse_removes_all_descendants`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a"), file("f.txt", "f.txt")))
            .toggle("a")
            .setChildren("a", listOf(dir("a/b", "a/b"), file("a/x.txt", "a/x.txt")))
            .toggle("a/b")
            .setChildren("a/b", listOf(file("a/b/c.txt", "a/b/c.txt")))

        assertEquals(
            listOf("a", "a/b", "a/b/c.txt", "a/x.txt", "f.txt"),
            state.visibleRows().map { it.path },
        )

        val collapsed = state.toggle("a").visibleRows()
        assertEquals(listOf("a", "f.txt"), collapsed.map { it.path })
        assertUniquePaths(collapsed)
    }

    @Test
    fun `nested_expansion_projects_depth_first_preorder`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("src", "src"), file("README.md", "README.md")))
            .toggle("src")
            .setChildren("src", listOf(dir("src/main", "src/main"), file("src/util.kt", "src/util.kt")))
            .toggle("src/main")
            .setChildren("src/main", listOf(file("src/main/App.kt", "src/main/App.kt")))

        assertEquals(
            listOf("src", "src/main", "src/main/App.kt", "src/util.kt", "README.md"),
            state.visibleRows().map { it.path },
        )
    }

    @Test
    fun `empty_directory_expands_to_no_child_rows`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("empty", "empty")))
            .toggle("empty")
            .setChildren("empty", emptyList())

        val rows = state.visibleRows()
        assertEquals(listOf("empty"), rows.map { it.path })
        assertFalse(rows.single().loading)
        assertUniquePaths(rows)
    }

    @Test
    fun `repeat_toggle_is_idempotent_without_duplicate_rows`() {
        val once = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a")))
            .toggle("a")
            .setChildren("a", listOf(file("a/x.txt", "a/x.txt")))

        val twice = once.toggle("a").toggle("a")

        assertEquals(once.visibleRows(), twice.visibleRows())
        assertUniquePaths(twice.visibleRows())
    }

    @Test
    fun `error_marks_row_and_keeps_prior_entries`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a")))
            .toggle("a")
            .setChildren("a", listOf(file("a/x.txt", "a/x.txt")))
            .setError("a", "boom")

        val row = state.visibleRows().first { it.path == "a" }
        assertEquals("boom", row.error)
        assertFalse(row.loading)
        assertEquals(listOf("a", "a/x.txt"), state.visibleRows().map { it.path })
    }

    @Test
    fun `mark_loading_keeps_entries_and_flags_directory_row`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a")))
            .toggle("a")
            .setChildren("a", listOf(file("a/x.txt", "a/x.txt")))
            .setLoading("a", true)

        val row = state.visibleRows().first { it.path == "a" }
        assertTrue(row.loading)
        assertEquals(listOf("a", "a/x.txt"), state.visibleRows().map { it.path })
    }

    @Test
    fun `start_loading_clears_previous_error`() {
        val state = TreeState()
            .setChildren(ROOT_PATH, listOf(dir("a", "a")))
            .setError("a", "boom")
            .setLoading("a", true)

        val row = state.visibleRows().first { it.path == "a" }
        assertTrue(row.loading)
        assertNull(row.error)
    }

    @Test
    fun `unknown_path_operations_do_not_corrupt_visible_tree`() {
        val base = TreeState().setChildren(ROOT_PATH, listOf(file("f.txt", "f.txt")))
        val before = base.visibleRows()

        val after = base
            .toggle("ghost")
            .setChildren("ghost", listOf(file("ghost/x.txt", "ghost/x.txt")))
            .setError("other", "boom")

        assertEquals(before, after.visibleRows())
        assertTrue("ghost" in after.expandedPaths)
        assertUniquePaths(after.visibleRows())
    }

    private fun assertUniquePaths(rows: List<TreeRow>) {
        assertEquals(rows.size, rows.map { it.path }.toSet().size)
    }
}
