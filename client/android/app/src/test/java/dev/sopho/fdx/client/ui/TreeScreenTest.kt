package dev.sopho.fdx.client.ui

import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.click
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import org.junit.Rule
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [35])
class TreeScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    private val jsonHeaders = headersOf("Content-Type", "application/json")

    @Test
    fun `file row requests only root and keeps full click target`() {
        val requested = mutableListOf<String>()
        val engine = MockEngine { request ->
            requested += request.url.parameters["path"] ?: "<missing>"
            respond(
                """{"path":"","entries":[{"name":"a.txt","path":"dir/a.txt","type":"file"}]}""",
                HttpStatusCode.OK,
                jsonHeaders,
            )
        }
        val client = mockClient(engine)
        var clickedPath: String? = null

        try {
            composeRule.setContent {
                TreeScreen(
                    client = client,
                    projectName = "proj",
                    modifier = Modifier.width(320.dp),
                    onFileClick = { clickedPath = it },
                )
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("a.txt")).fetchSemanticsNodes().isNotEmpty()
            }

            val fileRow = composeRule.onNode(hasText("a.txt") and hasClickAction())
            fileRow.assertWidthIsEqualTo(288.dp)
            fileRow.assertHeightIsAtLeast(48.dp)
            fileRow.performTouchInput { click(centerRight) }
            composeRule.runOnIdle {
                assertEquals("dir/a.txt", clickedPath)
                assertEquals(listOf(""), requested)
            }
        } finally {
            client.close()
        }
    }

    @Test
    fun `root failure retries through children endpoint`() {
        val requested = mutableListOf<String>()
        var calls = 0
        val engine = MockEngine { request ->
            requested += request.url.parameters["path"] ?: "<missing>"
            assertEquals("/api/projects/proj/tree/children", request.url.encodedPath)
            if (calls++ == 0) {
                respond("boom", HttpStatusCode.InternalServerError)
            } else {
                respond(
                    """{"path":"","entries":[{"name":"README.md","path":"README.md","type":"file"}]}""",
                    HttpStatusCode.OK,
                    jsonHeaders,
                )
            }
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                TreeScreen(client = client, projectName = "proj")
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("加载失败", substring = true)).fetchSemanticsNodes().isNotEmpty()
            }

            composeRule.onNodeWithText("重试").performClick()
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("README.md")).fetchSemanticsNodes().isNotEmpty()
            }
            composeRule.runOnIdle {
                assertEquals(listOf("", ""), requested)
            }
        } finally {
            client.close()
        }
    }

    @Test
    fun `directory click loads only that directory`() {
        val requested = mutableListOf<String>()
        val engine = MockEngine { request ->
            val path = request.url.parameters["path"] ?: "<missing>"
            requested += path
            val body = if (path.isEmpty()) {
                """{"path":"","entries":[{"name":"src","path":"src","type":"directory"},{"name":"docs","path":"docs","type":"directory"}]}"""
            } else {
                """{"path":"src","entries":[{"name":"Main.kt","path":"src/Main.kt","type":"file"}]}"""
            }
            respond(body, HttpStatusCode.OK, jsonHeaders)
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                TreeScreen(client = client, projectName = "proj")
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("src")).fetchSemanticsNodes().isNotEmpty()
            }

            composeRule.onNode(hasText("src") and hasClickAction()).performClick()
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("Main.kt")).fetchSemanticsNodes().isNotEmpty()
            }
            composeRule.runOnIdle {
                assertEquals(listOf("", "src"), requested)
            }
        } finally {
            client.close()
        }
    }

    @Test
    fun `failed directory keeps tree and retries locally`() {
        val requested = mutableListOf<String>()
        var srcCalls = 0
        val engine = MockEngine { request ->
            val path = request.url.parameters["path"] ?: "<missing>"
            requested += path
            when {
                path.isEmpty() -> respond(
                    """{"path":"","entries":[{"name":"src","path":"src","type":"directory"},{"name":"README.md","path":"README.md","type":"file"}]}""",
                    HttpStatusCode.OK,
                    jsonHeaders,
                )
                srcCalls++ == 0 -> respond("boom", HttpStatusCode.InternalServerError)
                else -> respond(
                    """{"path":"src","entries":[{"name":"Main.kt","path":"src/Main.kt","type":"file"}]}""",
                    HttpStatusCode.OK,
                    jsonHeaders,
                )
            }
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                TreeScreen(client = client, projectName = "proj")
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("src")).fetchSemanticsNodes().isNotEmpty()
            }

            composeRule.onNode(hasText("src") and hasClickAction()).performClick()
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("加载失败", substring = true)).fetchSemanticsNodes().isNotEmpty()
            }
            composeRule.onNodeWithText("README.md").assertExists()

            composeRule.onNodeWithText("重试").performClick()
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("Main.kt")).fetchSemanticsNodes().isNotEmpty()
            }
            composeRule.runOnIdle {
                assertEquals(listOf("", "src", "src"), requested)
            }
        } finally {
            client.close()
        }
    }
}
