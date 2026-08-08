package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme

/**
 * 导航目的地：sealed class 表示每个屏 + 它所需的参数。
 *
 * - [Config]：配置/连接页（栈底，根屏）。
 * - [ProjectList]：项目列表（连接成功后进入）。
 * - [Tree]：某项目的文件树（点项目进入，带 projectName）。
 *
 * 后续加 FileContent / Diff 屏时，在这里加一个子类即可。
 */
sealed class Destination {
    data object Config : Destination()

    data object ProjectList : Destination()

    data class Tree(val projectName: String) : Destination()
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    // 连接得到的 client：ProjectList / Tree 都靠它请求 daemon。
                    // 连接成功建一次，随导航在各屏间传递（与原逻辑一致，只是改为按栈顶渲染）。
                    var client by remember { mutableStateOf<ViewerClient?>(null) }

                    // 手动返回栈：栈底恒为 Config（根屏）。navigate push，popBack pop。
                    val backStack = remember { mutableStateListOf<Destination>(Destination.Config) }

                    fun navigate(dest: Destination) {
                        // 同目的地不重复压栈（避免连点造成栈里塞多个相同项）。
                        if (backStack.last() != dest) backStack.add(dest)
                    }

                    fun popBack(): Boolean {
                        if (backStack.size <= 1) return false
                        backStack.removeAt(backStack.lastIndex)
                        return true
                    }

                    // 系统返回键：栈深 > 1 时拦截，回到上一屏；仅根屏时放行系统默认（退出 App）。
                    BackHandler(enabled = backStack.size > 1) { popBack() }

                    // 按栈顶渲染，加细微过渡（淡入淡出）。
                    // 注：这里用对称 fade 而非带方向的横滑——AnimatedContent 的 transitionSpec
                    // 仅凭 targetState/initialState 无法可靠区分前进/后退，带方向会出现反向动画。
                    // 后续若需要方向化过渡，可在 navigate/popBack 时记一个状态再据此选择 enter/exit。
                    AnimatedContent(
                        targetState = backStack.last(),
                        transitionSpec = { fadeIn() togetherWith fadeOut() },
                        label = "navTransition",
                    ) { dest ->
                        when (dest) {
                            is Destination.Config -> ConfigScreen(
                                repo = store,
                                storagePath = applicationContext.filesDir.absolutePath,
                                onConnected = { conn ->
                                    client = ViewerClient.fromConnection(conn)
                                    navigate(Destination.ProjectList)
                                },
                            )

                            is Destination.ProjectList -> {
                                // client 在 onConnected 中先于 navigate(ProjectList) 赋值，
                                // 故到达此分支时 client 非空（与原 when 中 c != null 同义）。
                                val c = client!!
                                ProjectListScreen(
                                    client = c,
                                    onProjectClick = { name -> navigate(Destination.Tree(name)) },
                                )
                            }

                            is Destination.Tree -> {
                                val c = client!!
                                TreeScreen(client = c, projectName = dest.projectName)
                            }
                        }
                    }
                }
            }
        }
    }
}
