package dev.sopho.fdx.client.data

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * `Connection.isValid`（地址/token 非空校验）单测。
 *
 * 规则（见 Connection.kt）：`url.isNotBlank() && token.isNotBlank()`
 * —— 仅判非空白，不做格式校验。ZT 配置另有 `zerotier.isValid`。
 */
class ConnectionTest {

    @Test
    fun `url 和 token 都非空时有效`() {
        assertTrue(Connection("http://192.168.1.2:7321", "tok-abc").isValid)
    }

    @Test
    fun `url 为空时无效`() {
        assertFalse(Connection("", "tok-abc").isValid)
    }

    @Test
    fun `token 为空时无效`() {
        assertFalse(Connection("http://192.168.1.2:7321", "").isValid)
    }

    @Test
    fun `url 和 token 都空时无效`() {
        assertFalse(Connection("", "").isValid)
    }

    @Test
    fun `纯空白（空格、制表符、换行）视为空`() {
        // isNotBlank 把仅含空白字符的字符串判为空
        assertFalse(Connection("   ", "tok-abc").isValid)
        assertFalse(Connection("http://192.168.1.2:7321", "\t\n ").isValid)
    }

    @Test
    fun `默认值（均空）无效`() {
        // Connection() 默认 url="" token=""
        assertFalse(Connection().isValid)
    }

    @Test
    fun `isValid 与 zerotier 配置无关（zerotier 不影响 Connection 自身校验）`() {
        // 即便 zerotier.enabled=true 且 networkId 空（zerotier 无效），Connection.isValid 仍只看 url/token
        val conn = Connection(
            url = "http://192.168.1.2:7321",
            token = "tok",
            zerotier = ZerotierConfig(enabled = true, networkId = ""),
        )
        assertTrue(conn.isValid)
    }
}

/**
 * `ZerotierConfig.isValid` 单测。
 *
 * 规则（见 Connection.kt）：`!enabled || networkId.isNotBlank()`
 * —— enabled=false（普通 HTTP）恒有效；enabled=true 时 networkId 必填。
 */
class ZerotierConfigTest {

    @Test
    fun `未启用 ZT 时恒有效（networkId 空也行）`() {
        assertTrue(ZerotierConfig(enabled = false, networkId = "").isValid)
    }

    @Test
    fun `启用 ZT 且 networkId 非空时有效`() {
        assertTrue(ZerotierConfig(enabled = true, networkId = "a84ac5c10a1b2c3d").isValid)
    }

    @Test
    fun `启用 ZT 但 networkId 为空时无效`() {
        assertFalse(ZerotierConfig(enabled = true, networkId = "").isValid)
    }

    @Test
    fun `启用 ZT 但 networkId 仅空白时无效`() {
        assertFalse(ZerotierConfig(enabled = true, networkId = "   ").isValid)
    }

    @Test
    fun `默认值（未启用）有效`() {
        assertTrue(ZerotierConfig().isValid)
    }
}
