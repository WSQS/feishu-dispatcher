package dev.sopho.fdx.client.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import org.junit.Rule
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [35])
class FileContentScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    private val jsonHeaders = headersOf("Content-Type", "application/json")

    @Test
    fun `binary_file_shows_unavailable_message`() {
        val engine = MockEngine {
            respond(
                """{"path":"a.bin","rev":"work","binary":true,"content":""}""",
                HttpStatusCode.OK,
                jsonHeaders,
            )
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                FileContentScreen(client = client, projectName = "proj", path = "a.bin")
            }

            waitForText("（二进制文件，无法预览）")
            composeRule.onNodeWithText("（二进制文件，无法预览）").assertIsDisplayed()
        } finally {
            client.close()
        }
    }

    @Test
    fun `empty_file_shows_empty_message`() {
        val engine = MockEngine {
            respond(
                """{"path":"empty.txt","rev":"work","binary":false,"content":""}""",
                HttpStatusCode.OK,
                jsonHeaders,
            )
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                FileContentScreen(client = client, projectName = "proj", path = "empty.txt")
            }

            waitForText("（空文件）")
            composeRule.onNodeWithText("（空文件）").assertIsDisplayed()
        } finally {
            client.close()
        }
    }

    @Test
    fun `multi_line_file_renders_each_line`() {
        val engine = MockEngine {
            respond(
                """{"path":"a.txt","rev":"work","binary":false,"content":"hello\nworld"}""",
                HttpStatusCode.OK,
                jsonHeaders,
            )
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                FileContentScreen(client = client, projectName = "proj", path = "a.txt")
            }

            waitForText("hello")
            waitForText("world")
            composeRule.onNodeWithText("hello").assertIsDisplayed()
            composeRule.onNodeWithText("world").assertIsDisplayed()
        } finally {
            client.close()
        }
    }

    @Test
    fun `line_numbers_rendered_for_each_line`() {
        val engine = MockEngine {
            respond(
                """{"path":"a.txt","rev":"work","binary":false,"content":"hello\nworld"}""",
                HttpStatusCode.OK,
                jsonHeaders,
            )
        }
        val client = mockClient(engine)

        try {
            composeRule.setContent {
                FileContentScreen(client = client, projectName = "proj", path = "a.txt")
            }

            waitForText("1")
            waitForText("2")
            composeRule.onNodeWithText("1").assertIsDisplayed()
            composeRule.onNodeWithText("2").assertIsDisplayed()
        } finally {
            client.close()
        }
    }

    private fun waitForText(text: String) {
        composeRule.waitUntil(timeoutMillis = 5_000) {
            composeRule.onAllNodes(hasText(text)).fetchSemanticsNodes().isNotEmpty()
        }
    }
}
