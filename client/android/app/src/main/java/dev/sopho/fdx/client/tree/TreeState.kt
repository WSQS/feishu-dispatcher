package dev.sopho.fdx.client.tree

import dev.sopho.fdx.client.network.TreeChildrenEntry

/** workspace 根目录的路径表示（空串 = 根）。 */
const val ROOT_PATH: String = ""

private const val TYPE_DIRECTORY = "directory"

/** 单个目录的加载状态。 */
data class DirectoryState(
    val entries: List<TreeChildrenEntry> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
)

/** 投影后的单个可见行。 */
data class TreeRow(
    val path: String,
    val name: String,
    val isDirectory: Boolean,
    val depth: Int,
    val loading: Boolean = false,
    val error: String? = null,
)

/**
 * 文件树状态：只维护已访问目录的状态与展开集合，并投影稳定可见行列表。
 * 纯 Kotlin、不依赖 Android/Compose，可在 JVM 单测中直接验证。
 */
data class TreeState(
    val directories: Map<String, DirectoryState> = mapOf(ROOT_PATH to DirectoryState()),
    val expandedPaths: Set<String> = setOf(ROOT_PATH),
) {
    /** 展开/折叠目录；未访问过的目录展开时置为加载占位。 */
    fun toggle(path: String): TreeState {
        val expanded = path in expandedPaths
        val dirs = if (path in directories) directories else directories + (path to DirectoryState(loading = true))
        val expandedSet = if (expanded) expandedPaths - path else expandedPaths + path
        return copy(directories = dirs, expandedPaths = expandedSet)
    }

    /** 加载成功：落地该目录的直接子项并清除 loading/error。 */
    fun setChildren(path: String, entries: List<TreeChildrenEntry>): TreeState =
        copy(directories = directories + (path to DirectoryState(entries = entries)))

    /** 标记目录加载中/结束；开始加载时清除旧错误。 */
    fun setLoading(path: String, loading: Boolean): TreeState {
        val dir = directories[path] ?: DirectoryState()
        return copy(
            directories = directories + (path to dir.copy(loading = loading, error = if (loading) null else dir.error)),
        )
    }

    /** 记录目录加载失败；保留已有子项，不因失败清空旧数据。 */
    fun setError(path: String, message: String): TreeState {
        val dir = directories[path] ?: DirectoryState()
        return copy(directories = directories + (path to dir.copy(loading = false, error = message)))
    }

    /** 投影稳定可见行：深度优先前序、同级保序；目录行附带其 loading/error。 */
    fun visibleRows(): List<TreeRow> {
        val rows = mutableListOf<TreeRow>()
        fun visit(dirPath: String, depth: Int) {
            val dir = directories[dirPath] ?: return
            for (entry in dir.entries) {
                val state = directories[entry.path]
                rows += TreeRow(
                    path = entry.path,
                    name = entry.name,
                    isDirectory = entry.type == TYPE_DIRECTORY,
                    depth = depth,
                    loading = state?.loading ?: false,
                    error = state?.error,
                )
                if (entry.type == TYPE_DIRECTORY && entry.path in expandedPaths) {
                    visit(entry.path, depth + 1)
                }
            }
        }
        if (ROOT_PATH in expandedPaths) visit(ROOT_PATH, 0)
        return rows
    }
}
