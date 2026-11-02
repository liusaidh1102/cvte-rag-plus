/**
 * @param  {String}  url
 * @param  {Boolean} isNoCaseSensitive 是否区分大小写
 * @return {Object}
 */
// import numeral from 'numeral';

import { Base64 } from 'js-base64';
import JSEncrypt from 'jsencrypt';

// 获取浏览器视口宽度
export const getWidth = () => {
  return { width: window.innerWidth };
};
// rsa非对称加密密码：jsencrypt
export const rsaPsw = (password: string) => {
  const pub =
    '-----BEGIN PUBLIC KEY-----MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp2Q2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOOUEfaS318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVKRNLJpL8w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK6olN4RevSfVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs2wIDAQAB-----END PUBLIC KEY-----';
  const encryptor = new JSEncrypt();

  encryptor.setPublicKey(pub);

  return encryptor.encrypt(Base64.encode(password));
};

export default {
  getWidth,
  rsaPsw,
};

// 提取文件后缀名，并且转化为小写
export const getFileExtension = (filename: string) =>
  filename.slice(filename.lastIndexOf('.') + 1).toLowerCase();
