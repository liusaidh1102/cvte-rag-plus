export interface LoginRequestData {
  /** 用户名或邮箱 */
  username: string
  /** 密码 */
  password: string
  /** 登录端；管理后台固定为 management */
  channel?: "web" | "management"
  /** 管理后台使用明文密码，前台使用 RSA 密文 */
  password_encrypted?: boolean
}

export type CaptchaResponseData = ApiResponseData<string>

export type LoginResponseData = ApiResponseData<{
  token: string
  access_token?: string
  expiresIn?: number
  role?: string
  username?: string
  tenantId?: string | null
}>
