from typing import Dict, Optional, Type


class XBDMError(Exception):
    pass


class XBDMConnectionError(XBDMError):
    pass


class XBDMCommandError(XBDMError):
    code: Optional[int] = None
    _registry: Dict[int, Type["XBDMCommandError"]] = {}

    def __init_subclass__(cls, code: Optional[int] = None, **kwargs):
        super().__init_subclass__(**kwargs)
        if code is not None:
            cls.code = code
            XBDMCommandError._registry[code] = cls

    def __init__(self, code: Optional[int] = None, message: str = ""):
        if isinstance(code, str):
            message = code
            code = self.__class__.code or 0
        super().__init__(f"XBDM Error {code}: {message}")
        self.code = code
        self.message = message

    @classmethod
    def from_code(cls, code: int, message: str = "") -> "XBDMCommandError":
        exc_cls = cls._registry.get(code, cls)
        return exc_cls(code, message)


class XBDMUnexpectedError(XBDMCommandError, code=400):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 400
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)

class XBDMMaxConnectionsError(XBDMCommandError, code=401):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 401
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)

class XBDMFileNotFoundError(XBDMCommandError, code=402):
    def __init__(self, path_or_code: Optional[int] = None, message: str = ""):
        if isinstance(path_or_code, str) and not message:
            self.path = path_or_code
            super().__init__(402, f"File not found: {path_or_code}")
        else:
            self.path = message
            super().__init__(path_or_code, message)

class XBDMNoSuchModuleError(XBDMCommandError, code=403):
    def __init__(self, module_or_code: Optional[int] = None, message: str = ""):
        if isinstance(module_or_code, str) and not message:
            self.module = module_or_code
            super().__init__(403, f"No such module: {module_or_code}")
        else:
            self.module = message
            super().__init__(module_or_code, message)

class XBDMMemoryNotMappedError(XBDMCommandError, code=404):
    def __init__(self, address_or_code: Optional[int] = None, message: str = ""):
        if isinstance(address_or_code, str) and not message:
            self.address = address_or_code
            super().__init__(404, f"Memory not mapped: {address_or_code}")
        else:
            self.address = message
            super().__init__(address_or_code, message)

class XBDMNoSuchThreadError(XBDMCommandError, code=405):
    def __init__(self, thread_or_code: Optional[int] = None, message: str = ""):
        if isinstance(thread_or_code, str) and not message:
            self.thread = thread_or_code
            super().__init__(405, f"No such thread: {thread_or_code}")
        else:
            self.thread = message
            super().__init__(thread_or_code, message)

class XBDMSetSysTimeError(XBDMCommandError, code=406):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 406
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)

class XBDMUnknownCommandError(XBDMCommandError, code=407):
    def __init__(self, command_or_code: Optional[int] = None, message: str = ""):
            if isinstance(command_or_code, str) and not message:
                self.command = command_or_code
                super().__init__(407, f"Unknown command: {command_or_code}")
            else:
                self.command = message
                super().__init__(command_or_code, message)

class XBDMNotStoppedError(XBDMCommandError, code=408):  
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 408
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)

class XBDMFileMustBeCopiedError(XBDMCommandError, code=409):
    def __init__(self, path_or_code: Optional[int] = None, message: str = ""):
        if isinstance(path_or_code, str) and not message:
            self.path = path_or_code
            super().__init__(409, f"File must be copied: {path_or_code}")
        else:
            self.path = message
            super().__init__(path_or_code, message)

class XBDMFileAlreadyExistsError(XBDMCommandError, code=410):
    def __init__(self, path_or_code: Optional[int] = None, message: str = ""):
        if isinstance(path_or_code, str) and not message:
            self.path = path_or_code
            super().__init__(410, f"File already exists: {path_or_code}")
        else:
            self.path = message
            super().__init__(path_or_code, message)

class XBDMDirectoryNotEmptyError(XBDMCommandError, code=411):
    def __init__(self, path_or_code: Optional[int] = None, message: str = ""):
        if isinstance(path_or_code, str) and not message:
            self.path = path_or_code
            super().__init__(411, f"Directory not empty: {path_or_code}")
        else:
            self.path = message
            super().__init__(path_or_code, message)

class XBDMFilenameInvalidError(XBDMCommandError, code=412):
    def __init__(self, filename_or_code: Optional[int] = None, message: str = ""):
        if isinstance(filename_or_code, str) and not message:
            self.filename = filename_or_code
            super().__init__(412, f"Filename is invalid: {filename_or_code}")
        else:
            self.filename = message
            super().__init__(filename_or_code, message)

class XBDMFileCannotBeCreatedError(XBDMCommandError, code=413):
    def __init__(self, path_or_code: Optional[int] = None, message: str = ""):
        if isinstance(path_or_code, str) and not message:
            self.path = path_or_code
            super().__init__(413, f"File cannot be created: {path_or_code}")
        else:
            self.path = message
            super().__init__(path_or_code, message)

class XBDMAccessDeniedError(XBDMCommandError, code=414):
    def __init__(self, resource_or_code: Optional[int] = None, message: str = ""):
        if isinstance(resource_or_code, str) and not message:
            self.resource = resource_or_code
            super().__init__(414, f"Access denied: {resource_or_code}")
        else:
            self.resource = message
            super().__init__(resource_or_code, message)

class XBDMNoRoomOnDeviceError(XBDMCommandError, code=415):
    def __init__(self, resource_or_code: Optional[int] = None, message: str = ""):
        if isinstance(resource_or_code, str) and not message:
            self.resource = resource_or_code
            super().__init__(415, f"No room on device: {resource_or_code}")
        else:
            self.resource = message
            super().__init__(resource_or_code, message)

class XBDMNotDebuggableError(XBDMCommandError, code=416):
    def __init__(self, resource_or_code: Optional[int] = None, message: str = ""):
            if isinstance(resource_or_code, str) and not message:
                self.resource = resource_or_code
                super().__init__(416, f"Not debuggable: {resource_or_code}")
            else:
                self.resource = message
                super().__init__(resource_or_code, message)

class XBDMTypeInvalidError(XBDMCommandError, code=417):
    def __init__(self, type_or_code: Optional[int] = None, message: str = ""):
        if isinstance(type_or_code, str) and not message:
            self.type = type_or_code
            super().__init__(417, f"Type invalid: {type_or_code}")
        else:
            self.type = message
            super().__init__(type_or_code, message)

class XBDMDataNotAvailableError(XBDMCommandError, code=418):
    def __init__(self, data_or_code: Optional[int] = None, message: str = ""):
        if isinstance(data_or_code, str) and not message:
            self.data = data_or_code
            super().__init__(418, f"Data not available: {data_or_code}")
        else:
            self.data = message
            super().__init__(data_or_code, message)

class XBDMBoxNotLockedError(XBDMCommandError, code=420):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
            if isinstance(code_or_message, str) and not message:
                message = code_or_message
                code_or_message = 420
                super().__init__(code_or_message, message)
            else:
                super().__init__(code_or_message, message)

class XBDMKeyExchangeRequiredError(XBDMCommandError, code=421):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 421
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)

class XBDMDedicatedConnectionRequiredError(XBDMCommandError, code=422):
    def __init__(self, code_or_message: Optional[int] = None, message: str = ""):
        if isinstance(code_or_message, str) and not message:
            message = code_or_message
            code_or_message = 422
            super().__init__(code_or_message, message)
        else:
            super().__init__(code_or_message, message)
